"""Live HUD / statusline for Samantha's loop.

Renders one Rich-markup line each turn showing the operational state of the
loop: which model is driving, the reasoning effort, the current git branch,
context-window pressure, spend, and throughput.

Honesty principle (spec 4.8): Claude runs through ``claude -p`` on a Max
subscription, so the marginal *API* cost is $0. The real money is TTS, so we
surface ElevenLabs spend explicitly alongside the token counts.

This module is deliberately pure and dependency-light:

* :func:`render` takes a plain ``dict`` of stats and returns a string. It does
  no I/O -- no git, no subprocess, no clock -- so it is trivially testable and
  cheap to call every turn. The caller is responsible for assembling the dict
  (e.g. by calling :func:`git_branch` and :func:`tts_cost`).
* :func:`git_branch` is the only function that shells out, and it fails soft:
  it returns ``None`` for anything that isn't a clean git checkout.
* :func:`tts_cost` is closed-form arithmetic over published ElevenLabs rates.
"""

from __future__ import annotations

import subprocess
from typing import Any, Mapping, Optional

# ---------------------------------------------------------------------------
# TTS pricing
# ---------------------------------------------------------------------------
#
# ElevenLabs bills by "credits". For the multilingual v2 model one credit maps
# to one character of synthesized text. The dollar value of a credit depends on
# the subscription tier (monthly price / monthly credit allotment):
#
#   Creator   $22 / 100,000 credits  -> $0.000220 / char
#   Starter   $5  / 30,000  credits  -> ~$0.000167 / char
#   Pro       $99 / 500,000 credits  -> $0.000198 / char
#
# Samantha's verified account is on the ``creator`` tier, so that is the
# default. Rates are dollars-per-character.
TTS_RATE_PER_CHAR: dict[str, float] = {
    "starter": 5.0 / 30_000,
    "creator": 22.0 / 100_000,
    "pro": 99.0 / 500_000,
}

DEFAULT_TTS_TIER = "creator"

# Default context budget, mirrored from config's ``compact_threshold_tokens``.
# Used only as a fallback when the caller omits ``ctx_threshold`` from stats.
DEFAULT_CTX_THRESHOLD = 24_000


def tts_cost(chars: int, tier: str = DEFAULT_TTS_TIER) -> float:
    """Estimate the ElevenLabs TTS cost in USD for ``chars`` characters.

    Args:
        chars: Number of characters that were (or will be) synthesized.
            Negative values are clamped to zero.
        tier: ElevenLabs subscription tier. One of ``"starter"``,
            ``"creator"``, ``"pro"``. Case-insensitive. Unknown tiers fall
            back to the creator rate.

    Returns:
        Estimated cost in US dollars. ``0.0`` for non-positive ``chars``.
    """
    if chars <= 0:
        return 0.0
    rate = TTS_RATE_PER_CHAR.get(tier.lower(), TTS_RATE_PER_CHAR[DEFAULT_TTS_TIER])
    return chars * rate


def git_branch(cwd: Optional[str] = None) -> Optional[str]:
    """Return the current git branch name, or ``None`` if unavailable.

    Shells out to ``git -C <cwd> rev-parse --abbrev-ref HEAD``. Fails soft:
    returns ``None`` when the directory is not a repository, git is not
    installed, the call times out, or in detached-HEAD state (where the command
    prints ``"HEAD"``).

    Args:
        cwd: Directory to inspect. Defaults to the process working directory.

    Returns:
        The branch name, or ``None`` on any failure / detached HEAD.
    """
    cmd = ["git"]
    if cwd is not None:
        cmd += ["-C", cwd]
    cmd += ["rev-parse", "--abbrev-ref", "HEAD"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        # git missing, permission denied, timeout, etc.
        return None

    if result.returncode != 0:
        return None

    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        # Empty output or detached HEAD -- nothing useful to show.
        return None
    return branch


def _fmt_tokens(n: int) -> str:
    """Compact a token count: 950 -> '950', 24000 -> '24.0k'."""
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}k"


def _fmt_cost(usd: float) -> str:
    """Format a small dollar amount, e.g. 0.0 -> '$0.0000', 0.123 -> '$0.1230'."""
    return f"${usd:.4f}"


def render(stats: Mapping[str, Any]) -> str:
    """Render the HUD as a single Rich-markup line.

    The line is composed of six segments separated by a dim ``·``::

        🤖 <model> · ⛏ <effort> · 🌿 <branch> · 🧠 <ctx>/<thr>
        · 💲 API $0 (Max) +TTS ~<cost> · 📊 <tokens> · ⚡ <tok/s>

    ``render`` performs no I/O. Supply a plain dict so it stays testable.

    Recognized keys (all optional, with sensible fallbacks):

        model (str):            model label, e.g. "Opus". Default "Opus".
        effort (str):           reasoning effort label, e.g. "high".
                                Default "default".
        branch (str | None):    git branch. ``None`` renders as "no-git".
        ctx_tokens (int):       estimated context tokens in play. Default 0.
        ctx_threshold (int):    compaction threshold. Default
                                ``DEFAULT_CTX_THRESHOLD``.
        tts_cost (float):       USD spent on TTS this session. If absent, it is
                                derived from ``tts_chars`` via :func:`tts_cost`.
        tts_chars (int):        characters synthesized (used only when
                                ``tts_cost`` is absent). Default 0.
        tts_tier (str):         tier for the derived cost. Default "creator".
        tokens (int):           tokens produced on the last turn. Default 0.
        tok_per_s (float):      throughput of the last turn. Default 0.0.

    Args:
        stats: Mapping of the keys above.

    Returns:
        A Rich-markup string (single line, no trailing newline).
    """
    model = str(stats.get("model", "Opus"))
    effort = str(stats.get("effort", "default"))

    branch = stats.get("branch")
    branch_label = branch if branch else "no-git"

    ctx_tokens = int(stats.get("ctx_tokens", 0))
    ctx_threshold = int(stats.get("ctx_threshold", DEFAULT_CTX_THRESHOLD))

    if "tts_cost" in stats:
        cost = float(stats["tts_cost"])
    else:
        cost = tts_cost(
            int(stats.get("tts_chars", 0)),
            str(stats.get("tts_tier", DEFAULT_TTS_TIER)),
        )

    tokens = int(stats.get("tokens", 0))
    tok_per_s = float(stats.get("tok_per_s", 0.0))

    # Highlight context usage in red once it crosses the compaction threshold.
    ctx_style = "bold red" if ctx_threshold and ctx_tokens > ctx_threshold else "cyan"
    ctx_segment = (
        f"🧠 [{ctx_style}]{_fmt_tokens(ctx_tokens)}[/{ctx_style}]"
        f"[dim]/{_fmt_tokens(ctx_threshold)}[/dim]"
    )

    segments = [
        f"🤖 [bold magenta]{model}[/bold magenta]",
        f"⛏ [yellow]{effort}[/yellow]",
        f"🌿 [green]{branch_label}[/green]",
        ctx_segment,
        f"💲 [dim]API $0 (Max)[/dim] +TTS ~[bold]{_fmt_cost(cost)}[/bold]",
        f"📊 [cyan]{_fmt_tokens(tokens)}[/cyan]",
        f"⚡ [blue]{tok_per_s:.1f} tok/s[/blue]",
    ]

    return " [dim]·[/dim] ".join(segments)
