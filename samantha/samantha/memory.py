"""Context memory and auto-compaction for Samantha.

Samantha owns her own context assembly: persona + a rolling summary of older
turns + the most recent turns kept verbatim. When the estimated context grows
past a threshold, older turns are folded into the summary and dropped, so the
thread is never lost but the prompt stays bounded.

This module is deliberately mechanical and side-effect free. The actual
summarization is performed by an *injected* callable (``summarize_fn``) so the
heavy lifting -- a Haiku/Claude call -- lives in ``brain.py`` and can be mocked
in tests. Nothing here touches Claude, the network, or the filesystem.
"""

from __future__ import annotations

from typing import Callable, Iterable, Sequence

# Default rough token estimate: ~4 characters per token. Good enough for a
# budget gate; we never need exact tokenization here.
_CHARS_PER_TOKEN = 4

# How a message's speaker is labelled when rendered into a prompt or a
# compaction transcript. Mirrors brain._build_prompt's labelling.
_USER_ROLE = "user"
_USER_LABEL = "User"
_ASSISTANT_LABEL = "Samantha"


def estimate_tokens(text: str) -> int:
    """Roughly estimate the number of tokens in ``text``.

    Uses the common heuristic of ~4 characters per token. This is intentionally
    cheap and approximate -- it drives a budget gate, not billing.

    Args:
        text: The text to estimate.

    Returns:
        A non-negative token estimate. Empty/whitespace text returns 0.
    """
    if not text:
        return 0
    return len(text) // _CHARS_PER_TOKEN


def _message_role(message: object) -> str:
    """Return the role of a message, supporting Message objects and dicts."""
    if isinstance(message, dict):
        return message.get("role", "")
    return getattr(message, "role", "")


def _message_content(message: object) -> str:
    """Return the content of a message, supporting Message objects and dicts."""
    if isinstance(message, dict):
        return message.get("content", "")
    return getattr(message, "content", "")


def _render_message(message: object) -> str:
    """Render a single message as ``"<Label>: <content>"`` for a transcript."""
    role = _message_role(message)
    label = _USER_LABEL if role == _USER_ROLE else _ASSISTANT_LABEL
    return f"{label}: {_message_content(message)}"


def context_tokens(persona: str, summary: str, history: Iterable[object]) -> int:
    """Estimate the total context size, in tokens, of an assembled prompt.

    The estimate covers the three parts Samantha sends to Claude: the persona
    system prompt, the rolling summary of folded-away turns (may be empty), and
    every message still kept verbatim in ``history``.

    Args:
        persona: The system/persona prompt text.
        summary: The current rolling summary (``""`` when nothing folded yet).
        history: The verbatim conversation messages. Each item may be a
            ``Message``-like object (with ``.role``/``.content``) or a mapping
            with ``"role"``/``"content"`` keys.

    Returns:
        The summed token estimate across persona, summary, and history.
    """
    total = estimate_tokens(persona) + estimate_tokens(summary)
    for message in history:
        total += estimate_tokens(_message_content(message))
    return total


def should_compact(tokens: int, threshold: int) -> bool:
    """Decide whether the context should be compacted.

    Args:
        tokens: The current estimated context size in tokens.
        threshold: The maximum context size to allow before compacting.

    Returns:
        True when ``tokens`` strictly exceeds ``threshold``.
    """
    return tokens > threshold


def build_compaction_prompt(messages: Sequence[object]) -> str:
    """Build the text handed to ``summarize_fn`` when folding old turns.

    Renders the messages as a labelled transcript and wraps them with an
    instruction to produce a compact, durable digest. The result is a plain
    string so the injected summarizer can be any ``str -> str`` callable.

    Args:
        messages: The older messages being folded away.

    Returns:
        A prompt instructing the summarizer to digest the transcript.
    """
    transcript = "\n".join(_render_message(m) for m in messages)
    return (
        "Condense the conversation excerpt below into a compact digest that "
        "preserves the facts, decisions, names, and open threads needed to "
        "continue the conversation. Write it as plain prose -- no markdown, no "
        "bullet points, no preamble. Keep it brief.\n\n"
        f"{transcript}"
    )


def compact(
    history: Sequence[object],
    summary: str,
    keep: int,
    summarize_fn: Callable[[str], str],
) -> tuple[str, list]:
    """Fold older messages into the rolling summary, keeping recent ones.

    The most recent ``keep`` messages stay verbatim. Everything before them is
    rendered into a compaction prompt, summarized via the injected
    ``summarize_fn``, and merged with any existing ``summary``. No Claude or
    network call happens here -- ``summarize_fn`` owns that.

    Args:
        history: The full verbatim history, oldest first.
        summary: The existing rolling summary (``""`` if none yet).
        keep: How many of the most recent messages to keep verbatim. Values
            ``<= 0`` keep nothing; values ``>= len(history)`` keep everything.
        summarize_fn: A callable that turns the compaction prompt into a digest.
            It is only invoked when there are messages to fold away.

    Returns:
        A ``(new_summary, kept_list)`` tuple. ``new_summary`` is the existing
        summary with the folded digest appended (or just the digest when there
        was no prior summary). ``kept_list`` is a fresh list of the retained
        messages, in original order.

    When there is nothing to fold (``keep`` covers the whole history, or the
    history is empty), ``summarize_fn`` is never called and the original
    ``summary`` is returned unchanged alongside a copy of ``history``.
    """
    keep = max(keep, 0)
    # Index splitting the folded span from the kept span. When ``keep`` covers
    # (or exceeds) the whole history there is nothing older to fold, so the
    # split lands at the very end.
    split = max(len(history) - keep, 0)

    older = list(history[:split])
    kept = list(history[split:])

    if not older:
        # Nothing to fold; leave the summary untouched.
        return summary, kept

    prompt = build_compaction_prompt(older)
    digest = summarize_fn(prompt).strip()

    if summary and digest:
        new_summary = f"{summary.strip()}\n{digest}"
    elif digest:
        new_summary = digest
    else:
        # Summarizer returned nothing usable; preserve the prior summary.
        new_summary = summary

    return new_summary, kept
