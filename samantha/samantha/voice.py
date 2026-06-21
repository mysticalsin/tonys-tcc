"""Voice engine -- speech recognition and audio playback.

Handles microphone input via SpeechRecognition (Google free STT) and audio
playback (``afplay`` / ``ffplay`` / ``mpv`` / ``aplay``). Text-to-speech itself
is no longer hardcoded here: it is delegated to a pluggable backend obtained via
:func:`samantha.tts.resolve_backend` (ElevenLabs by default, optionally the
local Miso model). :class:`VoiceEngine` keeps the microphone, the player, and
the per-session character count for the HUD; the backend owns synthesis.

TTS is considered available iff a backend resolved *and* reports itself
available -- not merely because a config field is truthy. All heavy imports
(``speech_recognition``, ``pyaudio``, and every backend's own dependencies) stay
lazy, so this module imports cleanly on a machine without them installed.
"""

from __future__ import annotations

from samantha import config as cfg
from samantha import tts


class VoiceEngine:
    """Manages speech-to-text (mic) and plays back synthesized audio.

    TTS synthesis is delegated to a :class:`samantha.tts.TTSBackend` selected
    from ``provider`` + ``settings``; the engine degrades gracefully to a
    text-only loop when no backend can speak.

    Args:
        provider: Requested TTS backend name (``"elevenlabs"`` or ``"miso"``).
            Unknown/empty maps to ElevenLabs. Defaults to the configured
            ``tts_provider``.
        settings: Backend configuration (voice ids, model ids, speed, miso_*).
            When omitted, the loaded config is used. ``resolve_key=False`` in
            this dict forces the ElevenLabs key off (used by ``--no-voice``).
        language: Language code for speech recognition.
        listen_timeout: Seconds to wait for speech to start before giving up.
        phrase_time_limit: Maximum seconds to record a single phrase.
    """

    def __init__(
        self,
        provider: str = "",
        settings: dict | None = None,
        language: str = "en-US",
        listen_timeout: int = 10,
        phrase_time_limit: int = 60,
    ) -> None:
        settings = dict(settings) if settings else cfg.load()
        self.provider = provider or settings.get("tts_provider", "elevenlabs")
        self.language = language
        self.listen_timeout = listen_timeout
        self.phrase_time_limit = phrase_time_limit

        # Resolve the backend now so the caller can surface any fallback note and
        # name the active backend in the banner. ``backend`` is None when nothing
        # can speak; ``backend_note`` explains a fallback (or why there's no voice).
        self.backend, self.backend_note = tts.resolve_backend(self.provider, settings)

        self._recognizer = None

    # ------------------------------------------------------------------ status

    @property
    def tts_available(self) -> bool:
        """TTS is available iff a backend resolved and reports itself available."""
        return self.backend is not None and self.backend.available

    @property
    def backend_name(self) -> str:
        """Name of the active TTS backend, or ``""`` when there is none."""
        return self.backend.name if self.backend is not None else ""

    @property
    def chars_spoken(self) -> int:
        """Characters synthesized this session (for the HUD cost segment).

        Read off the active backend, which tracks it (ElevenLabs). Backends that
        don't track characters (Miso, which is free) report ``0``.
        """
        return int(getattr(self.backend, "chars_spoken", 0) or 0)

    @property
    def stt_available(self) -> bool:
        """Check whether speech recognition is available."""
        try:
            import speech_recognition as sr  # noqa: F401
            return True
        except ImportError:
            return False

    # ------------------------------------------------------------------ TTS

    def generate_audio(self, text: str) -> str | None:
        """Synthesize ``text`` via the backend and return a playable file path.

        Returns ``None`` when no backend is available. Synthesis errors from the
        backend are wrapped in :class:`TTSError` so callers keep one exception
        type to catch.
        """
        if not self.tts_available:
            return None
        try:
            return self.backend.synth(text)
        except tts.TTSUnavailable:
            # Backend went unavailable between resolve and synth; degrade quietly.
            return None
        except Exception as e:
            raise TTSError(f"Text-to-speech failed: {e}") from e

    def speak(self, text: str) -> None:
        """Generate and play TTS. For simple usage."""
        path = self.generate_audio(text)
        if path:
            self._play_audio_file(path)

    def stop_speaking(self) -> None:
        """Stop any currently playing audio."""
        pass  # Audio cleanup handled by system player subprocess

    # -------------------------------------------------------------- playback

    def _play_audio_file(self, path: str) -> None:
        """Play an audio file using the best available system player."""
        import platform
        import subprocess

        if platform.system() == "Darwin":
            subprocess.run(["afplay", path], check=True, capture_output=True)
        else:
            for player in (
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path],
                ["mpv", "--no-video", "--really-quiet", path],
                ["aplay", path],
            ):
                try:
                    subprocess.run(player, check=True, capture_output=True)
                    return
                except (FileNotFoundError, subprocess.CalledProcessError):
                    continue
            raise RuntimeError("No audio player found. Install ffmpeg or mpv.")

    def play_audio(self, path: str) -> None:
        """Play an audio file."""
        self._play_audio_file(path)

    def get_audio_duration(self, path: str) -> float:
        """Estimate audio duration from file size (MP3 ~128kbps)."""
        import os
        return os.path.getsize(path) / 16000

    # ------------------------------------------------------------------ STT

    def _init_recognizer(self):
        """Lazily initialize the speech recognizer."""
        if self._recognizer is not None:
            return self._recognizer

        import speech_recognition as sr

        self._recognizer = sr.Recognizer()
        self._recognizer.pause_threshold = 3.0
        self._recognizer.phrase_threshold = 0.2
        self._recognizer.non_speaking_duration = 2.0
        self._recognizer.dynamic_energy_threshold = True
        self._recognizer.energy_threshold = 300
        return self._recognizer

    def listen(self) -> str | None:
        """Listen via the microphone and return transcribed text (or None)."""
        import speech_recognition as sr

        recognizer = self._init_recognizer()

        try:
            with sr.Microphone() as source:
                recognizer.adjust_for_ambient_noise(source, duration=0.5)
                audio = recognizer.listen(
                    source,
                    timeout=self.listen_timeout,
                    phrase_time_limit=self.phrase_time_limit,
                )
        except sr.WaitTimeoutError:
            return None
        except OSError as e:
            raise RuntimeError(
                f"Could not access the microphone: {e}. "
                "Check your audio input settings and permissions."
            ) from e

        try:
            text = recognizer.recognize_google(audio, language=self.language)
            return text.strip() if text else None
        except sr.UnknownValueError:
            return None
        except sr.RequestError as e:
            raise RuntimeError(
                f"Speech recognition service error: {e}. "
                "Check your internet connection."
            ) from e

    # -------------------------------------------------------------- cleanup

    def cleanup(self) -> None:
        """Release audio resources and remove the backend's temp files."""
        if self.backend is not None:
            try:
                self.backend.cleanup()
            except Exception:
                pass


class TTSError(Exception):
    """Raised when text-to-speech conversion or playback fails."""
