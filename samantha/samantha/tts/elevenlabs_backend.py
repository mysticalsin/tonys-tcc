"""ElevenLabs text-to-speech backend.

The historical default backend: cloud synthesis via ElevenLabs, returning an
MP3 file. The synthesis logic lives here (moved out of
:mod:`samantha.voice`); :class:`~samantha.voice.VoiceEngine` keeps the
microphone/STT and playback responsibilities.

The API key is resolved by :mod:`samantha.secrets` (env -> macOS Keychain ->
config), never read straight from config here. The backend is considered
:attr:`available` iff a key actually resolves -- not merely because a config
field is truthy.

The official ElevenLabs SDK is imported lazily; when it is not installed the
backend falls back to a stdlib-only REST call, so a bare ``pip install`` without
the SDK still synthesizes as long as a key is present.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

from samantha import config as cfg
from samantha import secrets
from samantha.tts import TTSUnavailable


class ElevenLabsBackend:
    """TTS backend that synthesizes speech with ElevenLabs.

    Args:
        settings: Configuration mapping. Recognized keys (all optional, each
            falling back to :data:`samantha.config.DEFAULTS`):

            * ``elevenlabs_api_key`` -- explicit key; when falsy the key is
              resolved via :func:`samantha.secrets.get_elevenlabs_key`. Pass an
              explicit ``""`` together with ``resolve_key=False`` to force the
              backend off.
            * ``resolve_key`` -- when ``True`` (default) and no explicit key is
              given, resolve one via secrets. Set ``False`` to keep it disabled.
            * ``tts_voice_id`` -- ElevenLabs voice id.
            * ``tts_model_id`` -- ElevenLabs model id.
            * ``speech_speed`` -- mapped to ``voice_settings.speed``.
    """

    name = "elevenlabs"

    def __init__(self, settings: Optional[dict] = None) -> None:
        settings = settings or {}

        explicit = settings.get("elevenlabs_api_key", "")
        resolve = settings.get("resolve_key", True)
        if explicit:
            self.api_key = explicit
        elif resolve:
            self.api_key = secrets.get_elevenlabs_key() or ""
        else:
            self.api_key = ""

        self.voice_id = settings.get("tts_voice_id") or cfg.DEFAULTS["tts_voice_id"]
        self.model_id = settings.get("tts_model_id") or cfg.DEFAULTS["tts_model_id"]
        self.speed = float(settings.get("speech_speed", 1.0))

        # Characters synthesized this session, for the HUD cost segment.
        self.chars_spoken = 0

        self._client = None  # None: not tried; False: SDK absent; else: client
        self._temp_dir: Optional[Path] = None

    # ------------------------------------------------------------------ status

    @property
    def available(self) -> bool:
        """Available iff an ElevenLabs key resolved."""
        return bool(self.api_key)

    @property
    def unavailable_reason(self) -> str:
        """Why the backend is unavailable (empty when available)."""
        if self.available:
            return ""
        return (
            "no ElevenLabs API key found (set ELEVENLABS_API_KEY, store it in "
            "the macOS Keychain, or add elevenlabs_api_key to config)"
        )

    # --------------------------------------------------------------- synthesis

    def _ensure_temp_dir(self) -> Path:
        """Create (once) and return this backend's temp directory."""
        if self._temp_dir is None:
            self._temp_dir = Path(tempfile.mkdtemp(prefix="samantha_tts_eleven_"))
        return self._temp_dir

    def _init_client(self) -> None:
        """Lazily initialize the ElevenLabs SDK client (if importable)."""
        if self._client is not None:
            return
        try:
            from elevenlabs.client import ElevenLabs
        except ImportError:
            try:
                from elevenlabs import ElevenLabs  # older/newer layout
            except ImportError:
                self._client = False  # SDK unavailable -> REST fallback
                return
        self._client = ElevenLabs(api_key=self.api_key)

    def _voice_settings(self):
        """Build voice settings, mapping ``speed`` -> ``voice_settings.speed``."""
        try:
            from elevenlabs import VoiceSettings

            return VoiceSettings(speed=self.speed)
        except Exception:
            return None

    def _synth_via_sdk(self, text: str) -> Optional[bytes]:
        """Synthesize via the official SDK. Returns bytes, or None if no SDK."""
        self._init_client()
        if not self._client:
            return None

        kwargs = dict(
            voice_id=self.voice_id,
            model_id=self.model_id,
            text=text,
            output_format="mp3_44100_128",
        )
        settings = self._voice_settings()
        if settings is not None:
            kwargs["voice_settings"] = settings

        audio = self._client.text_to_speech.convert(**kwargs)
        if isinstance(audio, (bytes, bytearray)):
            return bytes(audio)
        # Streaming/generator response -> collect chunks.
        return b"".join(
            chunk if isinstance(chunk, bytes) else bytes(chunk) for chunk in audio
        )

    def _synth_via_rest(self, text: str) -> bytes:
        """REST fallback when the SDK can't be imported. Uses stdlib only."""
        import json
        import urllib.request

        url = (
            "https://api.elevenlabs.io/v1/text-to-speech/"
            f"{self.voice_id}?output_format=mp3_44100_128"
        )
        payload = {
            "text": text,
            "model_id": self.model_id,
            "voice_settings": {"speed": self.speed},
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "xi-api-key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()

    def synth(self, text: str) -> str:
        """Synthesize ``text`` to an MP3 file and return its path.

        Tries the SDK first, then the stdlib REST fallback.

        Raises:
            TTSUnavailable: If no API key resolved.
            RuntimeError: If synthesis returns no audio or the request fails.
        """
        if not self.available:
            raise TTSUnavailable(self.name, self.unavailable_reason)

        try:
            audio = self._synth_via_sdk(text)
            if audio is None:
                audio = self._synth_via_rest(text)
        except Exception as exc:
            raise RuntimeError(f"ElevenLabs text-to-speech failed: {exc}") from exc

        if not audio:
            raise RuntimeError("ElevenLabs text-to-speech returned no audio.")

        self.chars_spoken += len(text)
        audio_path = self._ensure_temp_dir() / "response.mp3"
        audio_path.write_bytes(audio)
        return str(audio_path)

    # ----------------------------------------------------------------- cleanup

    def cleanup(self) -> None:
        """Delete temp files and the temp directory. Idempotent."""
        if self._temp_dir is None:
            return
        for f in self._temp_dir.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            self._temp_dir.rmdir()
        except OSError:
            pass
        self._temp_dir = None

    def close(self) -> None:
        """Alias for :meth:`cleanup`."""
        self.cleanup()
