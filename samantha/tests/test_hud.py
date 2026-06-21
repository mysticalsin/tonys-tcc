"""Offline unit tests for samantha.hud.

These tests never touch the mic, claude, the network, or a real git repo.
``render`` is exercised purely on fake stat dicts; ``tts_cost`` is checked
against closed-form arithmetic; ``git_branch`` is checked only for its
graceful-failure contract (against a directory that is guaranteed not to be a
git repo) plus a fully stubbed subprocess for the success/detached paths.
"""

from __future__ import annotations

import subprocess

import pytest

from samantha import hud


# ---------------------------------------------------------------------------
# tts_cost
# ---------------------------------------------------------------------------


def test_tts_cost_creator_rate():
    # Creator tier: $22 / 100,000 chars == $0.00022 per char.
    assert hud.tts_cost(100_000, "creator") == pytest.approx(22.0)
    assert hud.tts_cost(1_000, "creator") == pytest.approx(0.22)
    assert hud.tts_cost(1, "creator") == pytest.approx(22.0 / 100_000)


def test_tts_cost_defaults_to_creator():
    assert hud.tts_cost(50_000) == hud.tts_cost(50_000, "creator")


def test_tts_cost_tier_case_insensitive():
    assert hud.tts_cost(10_000, "CREATOR") == hud.tts_cost(10_000, "creator")


def test_tts_cost_other_tiers():
    assert hud.tts_cost(30_000, "starter") == pytest.approx(5.0)
    assert hud.tts_cost(500_000, "pro") == pytest.approx(99.0)


def test_tts_cost_unknown_tier_falls_back_to_creator():
    assert hud.tts_cost(10_000, "platinum") == hud.tts_cost(10_000, "creator")


def test_tts_cost_zero_and_negative_chars():
    assert hud.tts_cost(0) == 0.0
    assert hud.tts_cost(-5) == 0.0


# ---------------------------------------------------------------------------
# render -- formatting on fake stats (no git, no subprocess)
# ---------------------------------------------------------------------------


def _full_stats() -> dict:
    return {
        "model": "Opus",
        "effort": "high",
        "branch": "main",
        "ctx_tokens": 12_000,
        "ctx_threshold": 24_000,
        "tts_cost": 0.0123,
        "tokens": 950,
        "tok_per_s": 42.5,
    }


def test_render_returns_single_line():
    line = hud.render(_full_stats())
    assert isinstance(line, str)
    assert "\n" not in line


def test_render_contains_all_segments():
    line = hud.render(_full_stats())
    # Each labelled emoji segment is present.
    for emoji in ("🤖", "⛏", "🌿", "🧠", "💲", "📊", "⚡"):
        assert emoji in line
    assert "Opus" in line
    assert "high" in line
    assert "main" in line


def test_render_separator_count():
    # Seven segments -> six separators.
    line = hud.render(_full_stats())
    assert line.count("[dim]·[/dim]") == 6


def test_render_context_tokens_and_threshold():
    line = hud.render(_full_stats())
    assert "12.0k" in line
    assert "/24.0k" in line


def test_render_small_token_count_not_abbreviated():
    line = hud.render(_full_stats())
    # 950 < 1000 -> plain integer, not "0.9k".
    assert "950" in line
    assert "0.9k" not in line


def test_render_tts_cost_formatted_four_decimals():
    line = hud.render(_full_stats())
    assert "$0.0123" in line
    assert "API $0 (Max)" in line


def test_render_throughput_one_decimal():
    line = hud.render(_full_stats())
    assert "42.5 tok/s" in line


def test_render_branch_none_shows_no_git():
    stats = _full_stats()
    stats["branch"] = None
    line = hud.render(stats)
    assert "no-git" in line


def test_render_missing_branch_key_shows_no_git():
    stats = _full_stats()
    del stats["branch"]
    line = hud.render(stats)
    assert "no-git" in line


def test_render_uses_defaults_for_empty_stats():
    line = hud.render({})
    # Falls back without raising.
    assert "Opus" in line
    assert "default" in line
    assert "no-git" in line
    assert f"/{hud._fmt_tokens(hud.DEFAULT_CTX_THRESHOLD)}" in line


def test_render_derives_cost_from_chars_when_cost_absent():
    stats = _full_stats()
    del stats["tts_cost"]
    stats["tts_chars"] = 1_000  # creator -> $0.22
    line = hud.render(stats)
    assert "$0.2200" in line


def test_render_context_over_threshold_uses_red_style():
    stats = _full_stats()
    stats["ctx_tokens"] = 30_000
    stats["ctx_threshold"] = 24_000
    line = hud.render(stats)
    assert "bold red" in line
    assert "30.0k" in line


def test_render_context_under_threshold_no_red():
    line = hud.render(_full_stats())
    assert "bold red" not in line


# ---------------------------------------------------------------------------
# git_branch -- graceful behavior
# ---------------------------------------------------------------------------


def test_git_branch_non_repo_returns_none(tmp_path):
    # tmp_path is a fresh directory with no .git -> must return None, not raise.
    assert hud.git_branch(str(tmp_path)) is None


def test_git_branch_returns_none_when_git_missing(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise FileNotFoundError("git not installed")

    monkeypatch.setattr(hud.subprocess, "run", _boom)
    assert hud.git_branch("/anywhere") is None


def test_git_branch_returns_none_on_timeout(monkeypatch):
    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2)

    monkeypatch.setattr(hud.subprocess, "run", _timeout)
    assert hud.git_branch("/anywhere") is None


def test_git_branch_returns_none_on_nonzero_exit(monkeypatch):
    def _fail(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="fatal")

    monkeypatch.setattr(hud.subprocess, "run", _fail)
    assert hud.git_branch("/anywhere") is None


def test_git_branch_returns_none_on_detached_head(monkeypatch):
    def _detached(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="HEAD\n", stderr="")

    monkeypatch.setattr(hud.subprocess, "run", _detached)
    assert hud.git_branch("/anywhere") is None


def test_git_branch_returns_branch_name(monkeypatch):
    captured = {}

    def _ok(cmd, *_args, **_kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="feature/loop\n", stderr="")

    monkeypatch.setattr(hud.subprocess, "run", _ok)
    assert hud.git_branch("/some/dir") == "feature/loop"
    # Confirms we invoke the documented git incantation with -C.
    assert captured["cmd"] == [
        "git", "-C", "/some/dir", "rev-parse", "--abbrev-ref", "HEAD",
    ]


def test_git_branch_no_cwd_omits_dash_c(monkeypatch):
    captured = {}

    def _ok(cmd, *_args, **_kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="main\n", stderr="")

    monkeypatch.setattr(hud.subprocess, "run", _ok)
    assert hud.git_branch() == "main"
    assert "-C" not in captured["cmd"]
