"""Local Miso TTS 8B text-to-speech backend.

Wraps `mysticalsin/MisoTTS <https://huggingface.co/MisoLabs/MisoTTS>`_ to run
speech synthesis entirely on-device: free and private, but heavy. The model is
~35 GB and realistically wants a CUDA GPU; on a Mac it falls back to CPU
(Apple MPS is unsupported because the model relies on float64 ops MPS lacks),
which works but is *very* slow.

The Miso public API used here::

    from generator import load_miso_8b, Segment
    gen = load_miso_8b(device, model_path_or_repo_id, dtype)   # -> Generator
    audio = gen.generate(text, speaker, context,
                         max_audio_length_ms, temperature, topk)  # 1-D tensor
    # gen.sample_rate == 24_000
    torchaudio.save(path, audio.unsqueeze(0).cpu(), gen.sample_rate)

Every heavy dependency (``torch``, ``torchaudio``, the Miso ``generator``
module) is imported *lazily inside methods only* -- never at module top -- so
that importing this module, and merely probing :attr:`available`, never drags in
multi-gigabyte frameworks or fails on a machine that lacks them. The model is
loaded exactly once and cached on the instance.
"""

from __future__ import annotations

import importlib.util
import logging
import tempfile
from pathlib import Path
from typing import Optional

from samantha.tts import TTSUnavailable

logger = logging.getLogger(__name__)

# Canonical defaults for Miso settings.
_DEFAULT_REPO_ID = "MisoLabs/MisoTTS"
_DEFAULT_SPEAKER = 0
_DEFAULT_MAX_MS = 12_000

# Shown when the Miso/torch stack cannot be imported at all.
_SETUP_HELP = (
    "Miso TTS is not installed. To enable the local voice backend:\n"
    "  1. pip install torch torchaudio\n"
    "  2. clone MisoTTS and `pip install -e <path-to-MisoTTS>` "
    "(exposes the `generator` module)\n"
    "  3. the model weights are ~35 GB and download on first use\n"
    "  4. log in to Hugging Face (`huggingface-cli login`) for the gated "
    "meta-llama/Llama-3.2-1B tokenizer\n"
    "  5. a CUDA GPU is strongly recommended (CPU works but is very slow; "
    "Apple MPS is unsupported)."
)


class MisoBackend:
    """TTS backend that synthesizes speech locally with Miso TTS 8B.

    Args:
        settings: Configuration mapping. Recognized keys (all optional):

            * ``miso_repo_id`` -- HF repo id for the weights
              (default ``"MisoLabs/MisoTTS"``).
            * ``miso_model_path`` -- local path to a checkpoint/directory; when
              set it overrides ``miso_repo_id`` and avoids the download.
            * ``miso_speaker`` -- speaker id passed to ``generate`` (default 0).
            * ``miso_max_ms`` -- ``max_audio_length_ms`` cap (default 12000).

    Attributes:
        slow: Set ``True`` after the first load when running on CPU, signalling
            that synthesis will be very slow.
    """

    name = "miso"

    def __init__(self, settings: Optional[dict] = None) -> None:
        settings = settings or {}
        self.repo_id = settings.get("miso_repo_id") or _DEFAULT_REPO_ID
        self.model_path = settings.get("miso_model_path") or None
        self.speaker = int(settings.get("miso_speaker", _DEFAULT_SPEAKER))
        self.max_ms = float(settings.get("miso_max_ms", _DEFAULT_MAX_MS))

        # Populated lazily on first synth().
        self._generator = None  # cached Generator (load once)
        self._device: Optional[str] = None
        self.slow = False
        self._temp_dir: Optional[Path] = None

    # ------------------------------------------------------------------ status

    @staticmethod
    def _torch_module():
        """Import and return ``torch``, or ``None`` if unavailable.

        Lazy by design: kept out of module scope so import never costs anything
        until a Miso backend is actually exercised.
        """
        try:
            import torch  # noqa: PLC0415  (intentional lazy import)

            return torch
        except Exception:  # ImportError or a broken install
            return None

    @staticmethod
    def _deps_importable() -> bool:
        """Whether torch, torchaudio and the Miso ``generator`` module exist.

        Uses :func:`importlib.util.find_spec` for ``torchaudio``/``generator``
        so we don't pay their import cost just to probe availability; ``torch``
        is imported because we also need it to query the device.
        """
        if MisoBackend._torch_module() is None:
            return False
        for mod in ("torchaudio", "generator"):
            try:
                if importlib.util.find_spec(mod) is None:
                    return False
            except (ImportError, ValueError):
                return False
        return True

    @staticmethod
    def _pick_device(torch) -> str:
        """Choose the synthesis device: CUDA if present, else CPU.

        Apple MPS is deliberately skipped: Miso relies on float64 ops MPS does
        not support, so a Mac runs on CPU.
        """
        return "cuda" if torch.cuda.is_available() else "cpu"

    @property
    def available(self) -> bool:
        """Available iff torch + torchaudio + the Miso ``generator`` import.

        A usable device always exists (CPU is the floor), so device choice does
        not gate availability -- but a CPU-only host is flagged via
        :attr:`slow` once the model loads.
        """
        return self._deps_importable()

    @property
    def unavailable_reason(self) -> str:
        """Why the backend is unavailable (empty when available)."""
        if self.available:
            return ""
        return "torch/torchaudio or the MisoTTS `generator` module not importable"

    @property
    def slow_warning(self) -> str:
        """Human warning shown when running on CPU (empty otherwise)."""
        if not self.slow:
            return ""
        return (
            "Miso TTS is running on CPU (no CUDA GPU; Apple MPS unsupported). "
            "Synthesis will be very slow."
        )

    # --------------------------------------------------------------- synthesis

    def _ensure_temp_dir(self) -> Path:
        """Create (once) and return this backend's temp directory."""
        if self._temp_dir is None:
            self._temp_dir = Path(tempfile.mkdtemp(prefix="samantha_tts_miso_"))
        return self._temp_dir

    def _ensure_generator(self):
        """Load the Miso model exactly once, caching the Generator instance.

        Raises:
            TTSUnavailable: If torch/torchaudio/``generator`` cannot be imported
                (with the full setup help message).
        """
        if self._generator is not None:
            return self._generator

        torch = self._torch_module()
        if torch is None:
            raise TTSUnavailable(self.name, _SETUP_HELP)
        try:
            from generator import load_miso_8b  # noqa: PLC0415 (lazy)
        except Exception as exc:  # ImportError or transitive failure
            raise TTSUnavailable(self.name, _SETUP_HELP) from exc

        device = self._pick_device(torch)
        self._device = device
        self.slow = device == "cpu"
        if self.slow:
            logger.warning(self.slow_warning)

        self._generator = load_miso_8b(
            device=device,
            model_path_or_repo_id=self.model_path or self.repo_id,
            dtype=torch.bfloat16,
        )
        return self._generator

    def synth(self, text: str) -> str:
        """Synthesize ``text`` to a WAV file and return its path.

        Loads the model on first call (cached thereafter), generates a 1-D audio
        tensor at 24 kHz, and writes it via ``torchaudio.save``.

        Raises:
            TTSUnavailable: If the Miso/torch stack is not importable.
            RuntimeError: If generation or saving fails.
        """
        if not self.available:
            raise TTSUnavailable(self.name, _SETUP_HELP)

        generator = self._ensure_generator()

        try:
            import torchaudio  # noqa: PLC0415 (lazy)

            audio = generator.generate(
                text=text,
                speaker=self.speaker,
                context=[],
                max_audio_length_ms=self.max_ms,
                temperature=0.9,
                topk=50,
            )
            audio_path = self._ensure_temp_dir() / "response.wav"
            torchaudio.save(
                str(audio_path),
                audio.unsqueeze(0).cpu(),
                generator.sample_rate,
            )
        except TTSUnavailable:
            raise
        except Exception as exc:
            raise RuntimeError(f"Miso text-to-speech failed: {exc}") from exc

        return str(audio_path)

    # ----------------------------------------------------------------- cleanup

    def cleanup(self) -> None:
        """Drop the cached model and delete temp files. Idempotent."""
        self._generator = None
        if self._temp_dir is not None:
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
