"""Offline unit tests for the pluggable :mod:`samantha.tts` package.

Everything here is fully offline and fast: no network, no real ElevenLabs
calls, and -- critically -- no real Miso model load or weight download. The
ElevenLabs SDK is mocked, and ``torch``/``torchaudio``/``generator`` are
installed as fake modules in ``sys.modules`` so :class:`MisoBackend` exercises
its real control flow against stand-ins.

Coverage:
    * factory selection (elevenlabs / miso / unknown -> elevenlabs)
    * fallback when the chosen backend is unavailable (monkeypatched)
    * ElevenLabsBackend.synth with the SDK + key fully mocked
    * MisoBackend.synth with torch/torchaudio/generator mocked, asserting a WAV
      is written and the model loads exactly once
    * TTSUnavailable raised cleanly when Miso deps are absent
"""

from __future__ import annotations

import importlib.machinery
import sys
import types
from pathlib import Path

import pytest

from samantha import tts
from samantha.tts import TTSUnavailable, get_backend, resolve_backend
from samantha.tts.elevenlabs_backend import ElevenLabsBackend
from samantha.tts.miso_backend import MisoBackend


# ==========================================================================
# Factory selection
# ==========================================================================


@pytest.fixture(autouse=True)
def _force_eleven_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ElevenLabs resolve a (fake) key so it counts as available.

    Many tests rely on ElevenLabs being the working fallback. We stub the key
    resolver rather than touch the real environment/Keychain.
    """
    monkeypatch.setattr(
        "samantha.tts.elevenlabs_backend.secrets.get_elevenlabs_key",
        lambda: "test-key-123",
    )


def test_factory_selects_elevenlabs() -> None:
    backend = get_backend("elevenlabs", {})
    assert isinstance(backend, ElevenLabsBackend)
    assert backend.name == "elevenlabs"


def test_factory_selects_miso_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pretend the Miso deps import so the factory honors the request.
    monkeypatch.setattr(MisoBackend, "_deps_importable", staticmethod(lambda: True))
    backend = get_backend("miso", {})
    assert isinstance(backend, MisoBackend)
    assert backend.name == "miso"


def test_factory_unknown_provider_falls_back_to_elevenlabs() -> None:
    backend, note = resolve_backend("does-not-exist", {})
    assert isinstance(backend, ElevenLabsBackend)
    assert "does-not-exist" in note
    assert "elevenlabs" in note.lower()


def test_factory_empty_provider_defaults_to_elevenlabs() -> None:
    backend, note = resolve_backend("", {})
    assert isinstance(backend, ElevenLabsBackend)
    assert note == ""


# ==========================================================================
# Fallback behavior
# ==========================================================================


def test_miso_unavailable_falls_back_to_elevenlabs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Miso reports unavailable -> factory must fall back to elevenlabs + explain.
    monkeypatch.setattr(MisoBackend, "_deps_importable", staticmethod(lambda: False))
    backend, note = resolve_backend("miso", {})
    assert isinstance(backend, ElevenLabsBackend)
    assert "miso" in note
    assert "falling back to elevenlabs" in note


def test_no_backend_when_everything_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both Miso deps and the ElevenLabs key are gone -> (None, explanation).
    monkeypatch.setattr(MisoBackend, "_deps_importable", staticmethod(lambda: False))
    monkeypatch.setattr(
        "samantha.tts.elevenlabs_backend.secrets.get_elevenlabs_key",
        lambda: None,
    )
    backend, note = resolve_backend("miso", {})
    assert backend is None
    assert "no voice output" in note


def test_elevenlabs_unavailable_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requested == fallback, and it's unavailable: give up cleanly.
    monkeypatch.setattr(
        "samantha.tts.elevenlabs_backend.secrets.get_elevenlabs_key",
        lambda: None,
    )
    backend, note = resolve_backend("elevenlabs", {})
    assert backend is None
    assert "No TTS backend available" in note


# ==========================================================================
# ElevenLabsBackend.synth -- SDK + key fully mocked (no network)
# ==========================================================================


class _FakeTTSConvert:
    """Stand-in for ``client.text_to_speech`` recording the convert call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def convert(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return b"FAKE_MP3_BYTES"


class _FakeClient:
    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key
        self.text_to_speech = _FakeTTSConvert()


def test_elevenlabs_synth_via_sdk_writes_mp3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = ElevenLabsBackend({"tts_voice_id": "voiceX", "tts_model_id": "modelY"})
    assert backend.available

    fake = _FakeClient(api_key=backend.api_key)
    # Bypass the SDK import entirely by injecting the client + a no-op init.
    backend._client = fake
    monkeypatch.setattr(backend, "_init_client", lambda: None)
    monkeypatch.setattr(backend, "_voice_settings", lambda: None)

    path = backend.synth("hello world")

    assert Path(path).exists()
    assert Path(path).suffix == ".mp3"
    assert Path(path).read_bytes() == b"FAKE_MP3_BYTES"
    assert backend.chars_spoken == len("hello world")
    assert fake.text_to_speech.calls[0]["voice_id"] == "voiceX"
    assert fake.text_to_speech.calls[0]["model_id"] == "modelY"

    backend.cleanup()
    assert not Path(path).exists()


def test_elevenlabs_synth_raises_unavailable_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "samantha.tts.elevenlabs_backend.secrets.get_elevenlabs_key",
        lambda: None,
    )
    backend = ElevenLabsBackend({})
    assert not backend.available
    with pytest.raises(TTSUnavailable) as exc:
        backend.synth("hi")
    assert exc.value.provider == "elevenlabs"


# ==========================================================================
# MisoBackend.synth -- torch/torchaudio/generator fully mocked (no model load)
# ==========================================================================


class _FakeTensor:
    """Minimal 1-D tensor stand-in supporting the calls the backend makes."""

    def unsqueeze(self, dim):  # noqa: ANN001
        return self

    def cpu(self):
        return self


class _FakeGenerator:
    """Stand-in for the Miso ``Generator`` returned by ``load_miso_8b``."""

    sample_rate = 24_000

    def __init__(self) -> None:
        self.generate_calls: list[dict] = []

    def generate(self, **kwargs):  # noqa: ANN003
        self.generate_calls.append(kwargs)
        return _FakeTensor()


@pytest.fixture
def _fake_miso_stack(monkeypatch: pytest.MonkeyPatch):
    """Install fake torch / torchaudio / generator modules in sys.modules.

    Yields a small namespace carrying the fake generator instance and a counter
    so tests can assert the model is loaded exactly once and a WAV gets saved.
    """
    state = types.SimpleNamespace(
        generator=_FakeGenerator(),
        load_count=0,
        saved=[],
    )

    # --- fake torch ---
    fake_torch = types.ModuleType("torch")
    fake_torch.bfloat16 = "bfloat16"
    fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)

    # --- fake torchaudio ---
    fake_torchaudio = types.ModuleType("torchaudio")

    def _save(path, tensor, sr):  # noqa: ANN001
        state.saved.append((path, sr))
        Path(path).write_bytes(b"FAKE_WAV")

    fake_torchaudio.save = _save

    # --- fake generator module ---
    fake_generator = types.ModuleType("generator")

    def _load_miso_8b(device, model_path_or_repo_id, dtype):  # noqa: ANN001
        state.load_count += 1
        state.last_load = {
            "device": device,
            "model": model_path_or_repo_id,
            "dtype": dtype,
        }
        return state.generator

    fake_generator.load_miso_8b = _load_miso_8b

    class _Segment:  # the module also exposes Segment
        def __init__(self, speaker, text, audio):  # noqa: ANN001
            self.speaker, self.text, self.audio = speaker, text, audio

    fake_generator.Segment = _Segment

    # find_spec() consults sys.modules but reads __spec__; a hand-built
    # ModuleType has __spec__ = None, which makes find_spec raise. Give each
    # fake a minimal spec so MisoBackend._deps_importable() sees them.
    for name, mod in (
        ("torch", fake_torch),
        ("torchaudio", fake_torchaudio),
        ("generator", fake_generator),
    ):
        mod.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
        monkeypatch.setitem(sys.modules, name, mod)
    return state


def test_miso_synth_writes_wav_and_loads_once(_fake_miso_stack) -> None:
    backend = MisoBackend({"miso_speaker": 3, "miso_max_ms": 8000})
    assert backend.available  # deps "import" via the fakes

    path1 = backend.synth("first line")
    path2 = backend.synth("second line")

    assert Path(path1).exists() and Path(path1).suffix == ".wav"
    assert Path(path1).read_bytes() == b"FAKE_WAV"
    # Model loaded exactly once across two synth calls.
    assert _fake_miso_stack.load_count == 1
    # CPU device (cuda.is_available -> False) flags slow.
    assert backend.slow is True
    assert backend._device == "cpu"
    assert _fake_miso_stack.last_load["device"] == "cpu"
    # generate() got our settings + an empty context.
    gen_call = _fake_miso_stack.generator.generate_calls[0]
    assert gen_call["speaker"] == 3
    assert gen_call["max_audio_length_ms"] == 8000
    assert gen_call["context"] == []
    assert len(_fake_miso_stack.saved) == 2

    backend.cleanup()
    assert not Path(path1).exists()
    assert backend._generator is None


def test_miso_uses_local_model_path_when_set(_fake_miso_stack) -> None:
    backend = MisoBackend({"miso_model_path": "/models/miso-local"})
    backend.synth("hi")
    assert _fake_miso_stack.last_load["model"] == "/models/miso-local"


def test_miso_unavailable_when_deps_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force the dep probe to report nothing importable.
    monkeypatch.setattr(MisoBackend, "_deps_importable", staticmethod(lambda: False))
    backend = MisoBackend({})
    assert not backend.available
    with pytest.raises(TTSUnavailable) as exc:
        backend.synth("hello")
    assert exc.value.provider == "miso"
    msg = str(exc.value)
    assert "pip install" in msg
    assert "35 GB" in msg


def test_miso_unavailable_when_torch_import_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Deps "probe" as present, but the actual torch import fails at load time:
    # synth must still raise TTSUnavailable cleanly with the setup help.
    monkeypatch.setattr(MisoBackend, "_deps_importable", staticmethod(lambda: True))
    monkeypatch.setattr(MisoBackend, "_torch_module", staticmethod(lambda: None))
    backend = MisoBackend({})
    with pytest.raises(TTSUnavailable) as exc:
        backend.synth("hello")
    assert exc.value.provider == "miso"


# ==========================================================================
# Protocol conformance
# ==========================================================================


def test_backends_satisfy_protocol() -> None:
    assert isinstance(ElevenLabsBackend({}), tts.TTSBackend)
    assert isinstance(MisoBackend({}), tts.TTSBackend)
