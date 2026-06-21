"""Pluggable text-to-speech backends for Samantha.

Samantha speaks through a swappable TTS backend. The historical default is
ElevenLabs (cloud, low latency, costs per character); the alternative is a
local Miso TTS 8B model (free, private, but heavy and slow without a GPU).

A backend is anything that satisfies :class:`TTSBackend`: it can report whether
it is :attr:`~TTSBackend.available`, turn ``text`` into a path to a playable
audio file via :meth:`~TTSBackend.synth`, and release its resources via
:meth:`~TTSBackend.cleanup`.

Selection goes through :func:`get_backend` / :func:`resolve_backend`. The
factory is fail-soft: if the requested backend cannot run (no key, missing
deps, no usable device), it logs *why* and falls back to ElevenLabs; if even
ElevenLabs is unavailable it returns ``None`` so the caller can degrade to a
text-only loop. :func:`resolve_backend` additionally hands back a human-readable
``note`` explaining any fallback so the HUD/CLI can surface it.

Heavy backend dependencies (the ElevenLabs SDK, torch, torchaudio, the Miso
``generator`` module) are imported lazily inside the backend implementations, so
importing this package is cheap and never fails on a bare machine.
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

__all__ = [
    "TTSBackend",
    "TTSUnavailable",
    "get_backend",
    "resolve_backend",
]

# Canonical backend names this factory knows how to build. ``elevenlabs`` is
# the fallback target and must always be present.
_KNOWN = ("elevenlabs", "miso")
_FALLBACK = "elevenlabs"


class TTSUnavailable(Exception):
    """Raised when a backend cannot run and a human needs to act.

    The message is meant to be shown to the user: it explains what is missing
    (a key, a package, a model download, a GPU) and how to fix it. Carries the
    offending ``provider`` name so callers can log/branch on it.
    """

    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(message)


@runtime_checkable
class TTSBackend(Protocol):
    """Protocol every TTS backend implements.

    A backend is responsible for one thing: turning text into a playable audio
    file on disk. Construction must be cheap and side-effect free (no key reads
    that raise, no model loads) so that :attr:`available` can be queried before
    committing to a backend. Expensive setup is deferred to the first
    :meth:`synth` call.
    """

    @property
    def name(self) -> str:
        """Stable identifier for this backend (e.g. ``"elevenlabs"``)."""
        ...

    @property
    def available(self) -> bool:
        """Whether this backend can actually synthesize right now.

        Must not raise: a backend that cannot resolve its key or import its
        dependencies reports ``False`` rather than blowing up.
        """
        ...

    def synth(self, text: str) -> str:
        """Synthesize ``text`` and return a path to a playable audio file.

        The file is written under the backend's own temp directory and lives
        until :meth:`cleanup` is called. Raises :class:`TTSUnavailable` if the
        backend is not :attr:`available`, or a backend-specific error if
        synthesis itself fails.
        """
        ...

    def cleanup(self) -> None:
        """Release resources and delete any temp files. Idempotent."""
        ...

    def close(self) -> None:
        """Alias for :meth:`cleanup`."""
        ...


def _build(name: str, settings: dict) -> Optional[TTSBackend]:
    """Instantiate a backend by canonical name, or ``None`` if it can't be built.

    Construction is meant to be cheap and non-raising, but we guard anyway so a
    surprising import/ctor failure degrades to a fallback rather than crashing
    the caller.
    """
    try:
        if name == "elevenlabs":
            from samantha.tts.elevenlabs_backend import ElevenLabsBackend

            return ElevenLabsBackend(settings)
        if name == "miso":
            from samantha.tts.miso_backend import MisoBackend

            return MisoBackend(settings)
    except Exception as exc:  # pragma: no cover - defensive; ctors are cheap
        logger.warning("Failed to construct %r TTS backend: %s", name, exc)
        return None
    return None


def _reason(backend: Optional[TTSBackend], name: str) -> str:
    """Best-effort human reason a backend is unavailable."""
    if backend is None:
        return f"{name} backend could not be constructed"
    why = getattr(backend, "unavailable_reason", None)
    return str(why) if why else f"{name} not configured"


def get_backend(provider: str, settings: dict) -> Optional[TTSBackend]:
    """Build a usable TTS backend, falling back to ElevenLabs then ``None``.

    Resolution:
        1. Map ``provider`` to a backend (``"elevenlabs"`` ->
           :class:`~samantha.tts.elevenlabs_backend.ElevenLabsBackend`,
           ``"miso"`` -> :class:`~samantha.tts.miso_backend.MisoBackend`).
           Any unknown value maps to ElevenLabs.
        2. If the chosen backend is :attr:`~TTSBackend.available`, return it.
        3. Otherwise log why and fall back to ElevenLabs.
        4. If ElevenLabs is also unavailable, return ``None`` (caller degrades
           to a text-only loop).

    Args:
        provider: Requested backend name. Case-insensitive; unknown -> elevenlabs.
        settings: Backend configuration (voice ids, model ids, speed, miso_*).

    Returns:
        A ready, :attr:`~TTSBackend.available` backend, or ``None`` if nothing
        can speak.
    """
    backend, _note = resolve_backend(provider, settings)
    return backend


def resolve_backend(
    provider: str, settings: dict
) -> tuple[Optional[TTSBackend], str]:
    """Like :func:`get_backend`, but also explain any fallback.

    Returns:
        A ``(backend, note)`` tuple. ``note`` is empty when the requested
        backend was honored without surprises, otherwise a human-readable
        sentence describing the fallback (e.g. why Miso was skipped and
        ElevenLabs used instead). When nothing can speak, ``backend`` is
        ``None`` and ``note`` explains it.
    """
    settings = settings or {}
    requested = (provider or "").strip().lower()
    unknown = bool(requested) and requested not in _KNOWN
    chosen = requested if requested in _KNOWN else _FALLBACK

    primary = _build(chosen, settings)
    if primary is not None and primary.available:
        if unknown:
            note = f"Unknown TTS provider {provider!r}; using {primary.name}."
            logger.warning(note)
            return primary, note
        return primary, ""

    why = _reason(primary, chosen)

    # If the unavailable backend already is the fallback, there is nowhere to go.
    if chosen == _FALLBACK:
        note = f"No TTS backend available: {why}"
        logger.warning(note)
        return None, note

    fallback = _build(_FALLBACK, settings)
    if fallback is not None and fallback.available:
        note = f"{chosen!r} TTS unavailable ({why}); falling back to {_FALLBACK}."
        logger.warning(note)
        return fallback, note

    note = (
        f"{chosen!r} TTS unavailable ({why}) and {_FALLBACK} unavailable "
        f"({_reason(fallback, _FALLBACK)}); no voice output."
    )
    logger.warning(note)
    return None, note
