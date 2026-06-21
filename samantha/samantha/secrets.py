"""Secret resolution for Samantha.

Resolves the ElevenLabs API key from the most secure available source,
never persisting it inside the repository. The resolution order is:

    1. Environment variable ``ELEVENLABS_API_KEY``
    2. macOS Keychain (``security find-generic-password``)
    3. ``config.py`` value ``elevenlabs_api_key`` (``~/.samantha/config.yaml``)

The Keychain lookup is graceful: on non-macOS platforms, when the
``security`` binary is missing, or when the entry is simply not found, it
returns ``None`` instead of raising. Tony stores the key himself (Keychain
recommended); the app only ever reads it.
"""

from __future__ import annotations

import os
import platform
import subprocess

from samantha import config as cfg

# Keychain coordinates. These identify *which* generic-password item to read;
# they are not secrets themselves and are safe to commit.
KEYCHAIN_SERVICE = "samantha-loop"
KEYCHAIN_ACCOUNT = "elevenlabs"

ENV_VAR = "ELEVENLABS_API_KEY"
CONFIG_KEY = "elevenlabs_api_key"


def _get_from_env() -> str | None:
    """Read the key from the environment, if present and non-empty."""
    value = os.environ.get(ENV_VAR)
    return value if value else None


def _get_from_keychain() -> str | None:
    """Read the key from the macOS Keychain, gracefully returning ``None``.

    Runs ``security find-generic-password -s samantha-loop -a elevenlabs -w``.
    Returns ``None`` on any non-macOS platform, when the ``security`` binary is
    unavailable, when the item is not found, or on any subprocess failure --
    callers should never see an exception from this helper.
    """
    if platform.system() != "Darwin":
        return None

    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
                "-w",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        # `security` binary missing or otherwise unrunnable -- treat as not found.
        return None

    if result.returncode != 0:
        # Non-zero exit means the item was not found (or access denied).
        return None

    key = result.stdout.strip()
    return key or None


def _get_from_config() -> str | None:
    """Read the key from ``~/.samantha/config.yaml`` via ``config.py``."""
    value = cfg.get(CONFIG_KEY)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def get_elevenlabs_key() -> str | None:
    """Resolve the ElevenLabs API key from the first available source.

    Resolution order (first non-empty wins):
        1. Environment variable ``ELEVENLABS_API_KEY``
        2. macOS Keychain (service ``samantha-loop``, account ``elevenlabs``)
        3. ``config.py`` value ``elevenlabs_api_key``

    Returns:
        The resolved API key, or ``None`` if no source provides one.
    """
    for source in (_get_from_env, _get_from_keychain, _get_from_config):
        key = source()
        if key:
            return key
    return None


def store_key_command() -> str:
    """Return the Keychain command Tony runs to store his key himself.

    The returned string is a *template* with a ``<your-key>`` placeholder --
    it never contains a real key. Tony substitutes his key and runs it once;
    the app then reads the key back via :func:`get_elevenlabs_key`.

    Returns:
        The ``security add-generic-password`` command template.
    """
    return (
        f"security add-generic-password "
        f"-s {KEYCHAIN_SERVICE} "
        f"-a {KEYCHAIN_ACCOUNT} "
        f"-w <your-key>"
    )
