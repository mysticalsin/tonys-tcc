"""Offline unit tests for :mod:`samantha.secrets`.

These tests are fully offline: no network, no real Keychain, no subprocess
execution. The environment, the Keychain subprocess call, and the config
lookup are all monkeypatched so we can assert the resolution *order* in
isolation:

    env ELEVENLABS_API_KEY  ->  macOS Keychain  ->  config elevenlabs_api_key
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from samantha import secrets


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the env var never leaks in from the real environment."""
    monkeypatch.delenv(secrets.ENV_VAR, raising=False)


def _fake_security(returncode: int, stdout: str = ""):
    """Build a fake ``subprocess.run`` that mimics the ``security`` binary.

    Args:
        returncode: Exit code the fake ``security`` reports (0 == found).
        stdout: The key the fake Keychain emits on stdout when found.
    """

    def _run(cmd, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        # Sanity: the command we build must target the right item.
        assert cmd[:1] == ["security"]
        assert "find-generic-password" in cmd
        assert secrets.KEYCHAIN_SERVICE in cmd
        assert secrets.KEYCHAIN_ACCOUNT in cmd
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    return _run


# --------------------------------------------------------------------------
# Resolution order
# --------------------------------------------------------------------------


def test_env_wins_over_keychain_and_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env var takes precedence even when keychain and config also have keys."""
    monkeypatch.setenv(secrets.ENV_VAR, "env-key")
    monkeypatch.setattr(secrets.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        secrets.subprocess, "run", _fake_security(0, "keychain-key")
    )
    monkeypatch.setattr(secrets.cfg, "get", lambda key: "config-key")

    assert secrets.get_elevenlabs_key() == "env-key"


def test_keychain_wins_over_config_when_no_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no env var, the Keychain value is used before config."""
    monkeypatch.setattr(secrets.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        secrets.subprocess, "run", _fake_security(0, "keychain-key\n")
    )
    monkeypatch.setattr(secrets.cfg, "get", lambda key: "config-key")

    assert secrets.get_elevenlabs_key() == "keychain-key"


def test_config_used_when_env_and_keychain_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config is the last resort when env is unset and Keychain misses."""
    monkeypatch.setattr(secrets.platform, "system", lambda: "Darwin")
    # returncode 44 == item not found in `security`.
    monkeypatch.setattr(secrets.subprocess, "run", _fake_security(44, ""))
    monkeypatch.setattr(secrets.cfg, "get", lambda key: "config-key")

    assert secrets.get_elevenlabs_key() == "config-key"


def test_returns_none_when_no_source_has_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No env, no keychain hit, empty config -> ``None``."""
    monkeypatch.setattr(secrets.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(secrets.subprocess, "run", _fake_security(44, ""))
    monkeypatch.setattr(secrets.cfg, "get", lambda key: "")

    assert secrets.get_elevenlabs_key() is None


# --------------------------------------------------------------------------
# Env-var hygiene
# --------------------------------------------------------------------------


def test_blank_env_falls_through_to_keychain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty-string env var is treated as unset, not as a valid key."""
    monkeypatch.setenv(secrets.ENV_VAR, "")
    monkeypatch.setattr(secrets.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        secrets.subprocess, "run", _fake_security(0, "keychain-key")
    )
    monkeypatch.setattr(secrets.cfg, "get", lambda key: "config-key")

    assert secrets.get_elevenlabs_key() == "keychain-key"


# --------------------------------------------------------------------------
# Keychain graceful degradation
# --------------------------------------------------------------------------


def test_non_mac_skips_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a non-macOS platform the Keychain is skipped without subprocess."""
    monkeypatch.setattr(secrets.platform, "system", lambda: "Linux")

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("subprocess.run must not be called off macOS")

    monkeypatch.setattr(secrets.subprocess, "run", _boom)
    monkeypatch.setattr(secrets.cfg, "get", lambda key: "config-key")

    assert secrets.get_elevenlabs_key() == "config-key"
    assert secrets._get_from_keychain() is None


def test_keychain_missing_binary_is_graceful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing ``security`` binary yields ``None``, not an exception."""
    monkeypatch.setattr(secrets.platform, "system", lambda: "Darwin")

    def _raise(*args, **kwargs):  # noqa: ANN002, ANN003
        raise FileNotFoundError("security not found")

    monkeypatch.setattr(secrets.subprocess, "run", _raise)

    assert secrets._get_from_keychain() is None


def test_keychain_not_found_returncode_is_graceful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero ``security`` exit means 'not found' and returns ``None``."""
    monkeypatch.setattr(secrets.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(secrets.subprocess, "run", _fake_security(44, ""))

    assert secrets._get_from_keychain() is None


def test_keychain_strips_trailing_newline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``security -w`` emits a trailing newline; it must be stripped."""
    monkeypatch.setattr(secrets.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        secrets.subprocess, "run", _fake_security(0, "  sk-the-key  \n")
    )

    assert secrets._get_from_keychain() == "sk-the-key"


# --------------------------------------------------------------------------
# store_key_command template
# --------------------------------------------------------------------------


def test_store_key_command_is_a_safe_template() -> None:
    """The command template carries the right coordinates and no real key."""
    cmd = secrets.store_key_command()

    assert cmd.startswith("security add-generic-password")
    assert f"-s {secrets.KEYCHAIN_SERVICE}" in cmd
    assert f"-a {secrets.KEYCHAIN_ACCOUNT} " in cmd
    assert "-w <your-key>" in cmd
    # It must remain a placeholder template -- never a baked-in secret.
    assert "<your-key>" in cmd
    assert "sk-" not in cmd


def test_store_key_command_round_trips_into_find_coordinates() -> None:
    """The add/store command targets the same item the reader looks up."""
    cmd = secrets.store_key_command()
    assert secrets.KEYCHAIN_SERVICE in cmd
    assert secrets.KEYCHAIN_ACCOUNT in cmd
