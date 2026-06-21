"""Offline unit tests for samantha.memory.

Every test here runs without a mic, the claude CLI, or the network. The
summarization step is exercised through an injected fake so we can assert
exactly what it received and control what it returns.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from samantha import memory


@dataclass
class FakeMessage:
    """Mirror of brain.Message for shaping history in tests."""

    role: str
    content: str


def _convo(n: int) -> list[FakeMessage]:
    """Build ``n`` alternating user/samantha messages with distinct content."""
    out: list[FakeMessage] = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "samantha"
        out.append(FakeMessage(role=role, content=f"msg-{i}"))
    return out


# --------------------------------------------------------------------------- #
# estimate_tokens
# --------------------------------------------------------------------------- #

def test_estimate_tokens_is_len_over_four():
    assert memory.estimate_tokens("a" * 400) == 100
    assert memory.estimate_tokens("abcd") == 1
    assert memory.estimate_tokens("abc") == 0  # 3 // 4 == 0


def test_estimate_tokens_empty():
    assert memory.estimate_tokens("") == 0


# --------------------------------------------------------------------------- #
# context_tokens
# --------------------------------------------------------------------------- #

def test_context_tokens_sums_persona_summary_and_history():
    persona = "p" * 40       # 10 tokens
    summary = "s" * 80       # 20 tokens
    history = [
        FakeMessage("user", "u" * 20),       # 5 tokens
        FakeMessage("samantha", "a" * 40),   # 10 tokens
    ]
    # 10 + 20 + 5 + 10
    assert memory.context_tokens(persona, summary, history) == 45


def test_context_tokens_empty_summary_and_history():
    assert memory.context_tokens("p" * 16, "", []) == 4


def test_context_tokens_accepts_dict_messages():
    history = [{"role": "user", "content": "x" * 16}]  # 4 tokens
    assert memory.context_tokens("", "", history) == 4


# --------------------------------------------------------------------------- #
# should_compact
# --------------------------------------------------------------------------- #

def test_should_compact_strictly_above_threshold():
    assert memory.should_compact(24001, 24000) is True
    assert memory.should_compact(24000, 24000) is False  # equal does not trip
    assert memory.should_compact(0, 24000) is False


# --------------------------------------------------------------------------- #
# build_compaction_prompt
# --------------------------------------------------------------------------- #

def test_build_compaction_prompt_labels_speakers():
    prompt = memory.build_compaction_prompt(
        [FakeMessage("user", "hello"), FakeMessage("samantha", "hi Tony")]
    )
    assert "User: hello" in prompt
    assert "Samantha: hi Tony" in prompt


def test_build_compaction_prompt_handles_dicts():
    prompt = memory.build_compaction_prompt([{"role": "user", "content": "yo"}])
    assert "User: yo" in prompt


# --------------------------------------------------------------------------- #
# compact -- the core behaviour required by spec 4.4
# --------------------------------------------------------------------------- #

def test_compact_keeps_last_keep_messages_verbatim():
    history = _convo(10)
    new_summary, kept = memory.compact(
        history, summary="", keep=6, summarize_fn=lambda _: "DIGEST"
    )
    assert kept == history[-6:]
    # Returned list is a fresh copy, not the same object.
    assert kept is not history


def test_compact_folds_dropped_span_into_summary():
    history = _convo(10)
    new_summary, kept = memory.compact(
        history, summary="", keep=6, summarize_fn=lambda _: "DIGEST"
    )
    assert new_summary == "DIGEST"
    # The 4 oldest were dropped from the kept set.
    assert len(kept) == 6
    assert kept[0].content == "msg-4"


def test_compact_calls_summarize_fn_with_only_the_dropped_span():
    history = _convo(10)
    captured: dict[str, str] = {}

    def spy(prompt: str) -> str:
        captured["prompt"] = prompt
        return "DIGEST"

    memory.compact(history, summary="", keep=6, summarize_fn=spy)

    prompt = captured["prompt"]
    # The 4 dropped messages must appear...
    for i in range(4):
        assert f"msg-{i}" in prompt
    # ...and the 6 kept ones must NOT be in the compaction prompt.
    for i in range(4, 10):
        assert f"msg-{i}" not in prompt


def test_compact_appends_to_existing_summary():
    history = _convo(8)
    new_summary, kept = memory.compact(
        history, summary="PRIOR", keep=4, summarize_fn=lambda _: "NEW"
    )
    assert new_summary == "PRIOR\nNEW"
    assert kept == history[-4:]


def test_compact_noop_when_keep_covers_whole_history():
    history = _convo(5)
    called = False

    def must_not_run(_: str) -> str:
        nonlocal called
        called = True
        return "SHOULD NOT HAPPEN"

    new_summary, kept = memory.compact(
        history, summary="PRIOR", keep=5, summarize_fn=must_not_run
    )
    assert called is False
    assert new_summary == "PRIOR"
    assert kept == history
    assert kept is not history  # still a fresh copy


def test_compact_noop_on_empty_history():
    called = False

    def must_not_run(_: str) -> str:
        nonlocal called
        called = True
        return "x"

    new_summary, kept = memory.compact(
        [], summary="PRIOR", keep=6, summarize_fn=must_not_run
    )
    assert called is False
    assert new_summary == "PRIOR"
    assert kept == []


def test_compact_keep_zero_folds_everything():
    history = _convo(4)
    new_summary, kept = memory.compact(
        history, summary="", keep=0, summarize_fn=lambda _: "ALL"
    )
    assert kept == []
    assert new_summary == "ALL"


def test_compact_negative_keep_treated_as_zero():
    history = _convo(4)
    new_summary, kept = memory.compact(
        history, summary="", keep=-3, summarize_fn=lambda _: "ALL"
    )
    assert kept == []
    assert new_summary == "ALL"


def test_compact_first_summary_has_no_leading_newline():
    history = _convo(6)
    new_summary, _ = memory.compact(
        history, summary="", keep=2, summarize_fn=lambda _: "FIRST"
    )
    assert new_summary == "FIRST"
    assert not new_summary.startswith("\n")


def test_compact_blank_digest_preserves_prior_summary():
    history = _convo(6)
    new_summary, kept = memory.compact(
        history, summary="PRIOR", keep=2, summarize_fn=lambda _: "   "
    )
    # Whitespace-only digest is dropped; prior summary survives.
    assert new_summary == "PRIOR"
    assert kept == history[-2:]


# --------------------------------------------------------------------------- #
# Integration-flavoured: the threshold gate driving a compaction round.
# --------------------------------------------------------------------------- #

def test_threshold_gate_then_compact_reduces_context():
    persona = "P" * 4000           # 1000 tokens
    history = [FakeMessage("user", "X" * 4000) for _ in range(10)]  # 1000 each
    threshold = 5000

    before = memory.context_tokens(persona, "", history)
    assert memory.should_compact(before, threshold) is True

    new_summary, kept = memory.compact(
        history, summary="", keep=2, summarize_fn=lambda _: "tiny digest"
    )
    after = memory.context_tokens(persona, new_summary, kept)

    assert after < before
    assert memory.should_compact(after, threshold) is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
