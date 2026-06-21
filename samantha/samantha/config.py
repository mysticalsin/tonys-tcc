"""Configuration management for Samantha.

Stores and loads user preferences from ~/.samantha/config.yaml.

Note on secrets: the ElevenLabs API key is intentionally *not* folded into the
config dict from the environment here. ``samantha.secrets.get_elevenlabs_key()``
owns the resolution order (env -> macOS Keychain -> config), so duplicating the
env handling in this module would create a confusing double-precedence path. The
``elevenlabs_api_key`` DEFAULTS entry below is only the lowest-priority fallback
that ``secrets`` reads via :func:`get`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path.home() / ".samantha"
CONFIG_FILE = CONFIG_DIR / "config.yaml"

DEFAULTS: dict[str, Any] = {
    # Who Samantha is talking to. Injected into the persona each turn.
    "user_name": "Tony",
    # Which TTS backend speaks: "elevenlabs" (cloud, default) or "miso" (local).
    # Resolved through samantha.tts; unknown/unavailable falls back to elevenlabs.
    "tts_provider": "elevenlabs",
    # --- ElevenLabs backend (cloud, default) ---
    # Key is resolved by secrets.py; this entry is the lowest-priority fallback.
    "elevenlabs_api_key": "",
    "tts_voice_id": "21m00Tcm4TlvDq8ikWAM",  # Rachel (warm female, verified)
    "tts_model_id": "eleven_multilingual_v2",
    "tts_tier": "creator",  # ElevenLabs subscription tier (drives cost HUD)
    "speech_speed": 1.0,
    # --- Miso backend (local 8B model; optional, heavy) ---
    "miso_repo_id": "MisoLabs/MisoTTS",  # HF weights repo
    "miso_model_path": "",  # local checkpoint path; overrides miso_repo_id when set
    "miso_speaker": 0,  # speaker id passed to the generator
    "miso_max_ms": 12000,  # max synthesized audio length per response (ms)
    "language": "en-US",
    "max_history": 12,
    "listen_timeout": 10,
    "phrase_time_limit": 30,
    # Auto-compaction: fold older turns into a rolling summary past this budget.
    "compact_threshold_tokens": 24000,
    "compact_keep": 6,  # recent messages kept verbatim after a compaction
    # Models. claude_model drives the main think; summary_model is the cheap
    # Haiku call used for both compaction digests and the estimate gate.
    "claude_model": "opus",
    "summary_model": "claude-haiku-4-5-20251001",
}


def _ensure_config_dir() -> None:
    """Create the config directory if it doesn't exist."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load() -> dict[str, Any]:
    """Load configuration from disk, falling back to defaults.

    Environment variables override file values. Note: ``ELEVENLABS_API_KEY`` is
    deliberately *not* handled here -- ``samantha.secrets`` reads it directly so
    the resolution order stays in one place.
    """
    config = dict(DEFAULTS)

    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                stored = yaml.safe_load(f) or {}
            config.update(stored)
        except (yaml.YAMLError, OSError):
            pass  # Fall back to defaults silently

    return config


def save(config: dict[str, Any]) -> None:
    """Persist configuration to disk."""
    _ensure_config_dir()
    with open(CONFIG_FILE, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


def get(key: str) -> Any:
    """Get a single config value."""
    return load().get(key, DEFAULTS.get(key))


def set_key(key: str, value: Any) -> None:
    """Set a single config value and persist."""
    config = load()
    config[key] = value
    save(config)
