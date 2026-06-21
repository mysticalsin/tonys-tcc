"""Offline unit tests for :mod:`samantha.estimator` (spec 4.7).

No mic, no network, no real ``claude`` call -- the model is always injected as
``run_fn`` so these run anywhere, deterministically.
"""

from __future__ import annotations

import json

import pytest

from samantha import estimator
from samantha.estimator import (
    DEFAULT_MINUTES_EST,
    DEFAULT_TOKENS_EST,
    build_estimate_prompt,
    estimate,
    is_project_intent,
    speak_line,
)


# ---------------------------------------------------------------------------
# Intent detection -- positives
# ---------------------------------------------------------------------------

POSITIVE_INTENTS = [
    "build me a CLI that scrapes RSS feeds",
    "Build a dashboard for the sales numbers",
    "create a Python script to rename files",
    "Can you implement a retry decorator for me?",
    "make me an app that tracks my workouts",
    "make me a tool to convert markdown to pdf",
    "set up a FastAPI project with auth",
    "setup a postgres database for the project",
    "deploy this service to the cloud",
    "refactor the brain module so it's testable",
    "scaffold a new package with tests",
    "write a script to back up my photos",
    "write an app for habit tracking",
    "spin up a redis cache",
    "rebuild the parser from scratch",
    "generate a module that wraps the API",
    "code me a function to debounce events",
]


@pytest.mark.parametrize("text", POSITIVE_INTENTS)
def test_is_project_intent_positive(text):
    assert is_project_intent(text) is True, text


# ---------------------------------------------------------------------------
# Intent detection -- negatives (casual chat must not trip the gate)
# ---------------------------------------------------------------------------

NEGATIVE_INTENTS = [
    "How are you feeling today?",
    "What did you build today?",
    "Did you create anything interesting lately?",
    "Tell me about the weather.",
    "What do you think about jazz?",
    "I had a really long day at work.",
    "That makes me feel happy.",
    "Do you remember what we talked about yesterday?",
    "I want to build a relationship with you.",
    "Have you ever been to Paris?",
    "Good morning, Samantha.",
    "",
    "   ",
    "Thanks, that was helpful.",
    "What is the capital of France?",
]


@pytest.mark.parametrize("text", NEGATIVE_INTENTS)
def test_is_project_intent_negative(text):
    assert is_project_intent(text) is False, text


def test_casual_phrase_with_concrete_software_object_still_triggers():
    # "did you build" is casual, but a concrete software object + build verb is
    # a real instruction in disguise.
    assert is_project_intent("did you build the auth module yet? build it now") is True


# ---------------------------------------------------------------------------
# estimate() -- well-formed model output
# ---------------------------------------------------------------------------


def test_estimate_parses_clean_json():
    payload = {
        "tokens_est": 80000,
        "minutes_est": 12,
        "steps": ["plan", "write code", "test"],
    }

    def run_fn(prompt: str) -> str:
        assert "Task:" in prompt  # the task made it into the prompt
        return json.dumps(payload)

    est = estimate("build me a CLI tool", run_fn)
    assert est["tokens_est"] == 80000
    assert est["minutes_est"] == 12.0
    assert est["steps"] == ["plan", "write code", "test"]


def test_estimate_parses_json_with_prose_and_fences():
    raw = (
        "Sure! Here is my estimate:\n"
        "```json\n"
        '{"tokens_est": 55000, "minutes_est": 8.5, "steps": ["scaffold", "wire up"]}\n'
        "```\n"
        "Let me know if you want changes."
    )
    est = estimate("scaffold a package", lambda _p: raw)
    assert est["tokens_est"] == 55000
    assert est["minutes_est"] == 8.5
    assert est["steps"] == ["scaffold", "wire up"]


def test_estimate_recovers_embedded_object_without_fence():
    raw = 'Estimate => {"tokens_est": 12000, "minutes_est": 3, "steps": []} done'
    est = estimate("write a small script", lambda _p: raw)
    assert est["tokens_est"] == 12000
    assert est["minutes_est"] == 3.0
    assert est["steps"] == []


def test_estimate_coerces_string_numbers_in_json():
    raw = '{"tokens_est": "80k", "minutes_est": "12", "steps": "plan; build; test"}'
    est = estimate("build a tool", lambda _p: raw)
    assert est["tokens_est"] == 80000
    assert est["minutes_est"] == 12.0
    assert est["steps"] == ["plan", "build", "test"]


def test_estimate_float_tokens_rounded():
    raw = '{"tokens_est": 79999.6, "minutes_est": 10}'
    est = estimate("build a tool", lambda _p: raw)
    assert est["tokens_est"] == 80000
    assert isinstance(est["tokens_est"], int)


# ---------------------------------------------------------------------------
# estimate() -- malformed output -> heuristic fallback
# ---------------------------------------------------------------------------


def test_estimate_scrapes_numbers_from_prose():
    raw = "This will take around 80k tokens and about 12 minutes."
    est = estimate("build something", lambda _p: raw)
    assert est["tokens_est"] == 80000
    assert est["minutes_est"] == 12.0


def test_estimate_scrapes_steps_from_bullets():
    raw = (
        "Roughly 30000 tokens, maybe 5 minutes.\n"
        "- design the schema\n"
        "- write the migration\n"
        "* run the tests\n"
    )
    est = estimate("build a db layer", lambda _p: raw)
    assert est["tokens_est"] == 30000
    assert est["minutes_est"] == 5.0
    assert est["steps"] == [
        "design the schema",
        "write the migration",
        "run the tests",
    ]


def test_estimate_comma_grouped_tokens():
    raw = "Probably 120,000 tokens over roughly 18 mins."
    est = estimate("big build", lambda _p: raw)
    assert est["tokens_est"] == 120000
    assert est["minutes_est"] == 18.0


def test_estimate_total_garbage_returns_scaled_defaults():
    raw = "I love talking with you about this."  # no numbers, no JSON, no bullets
    task = "build me a really big complicated multi service platform thing"
    est = estimate(task, lambda _p: raw)
    # Must never crash and must return a valid, positive estimate.
    assert isinstance(est["tokens_est"], int) and est["tokens_est"] > 0
    assert isinstance(est["minutes_est"], float) and est["minutes_est"] > 0
    assert est["steps"] == []
    # Bigger task scales above the floor.
    assert est["tokens_est"] >= DEFAULT_TOKENS_EST


def test_estimate_empty_output_returns_defaults():
    est = estimate("build a tool", lambda _p: "")
    assert est["tokens_est"] == DEFAULT_TOKENS_EST
    assert est["minutes_est"] == DEFAULT_MINUTES_EST
    assert est["steps"] == []


def test_estimate_run_fn_raising_is_swallowed():
    def boom(_prompt: str) -> str:
        raise RuntimeError("model unreachable")

    est = estimate("build a tool", boom)
    assert est["tokens_est"] == DEFAULT_TOKENS_EST
    assert est["minutes_est"] == DEFAULT_MINUTES_EST
    assert est["steps"] == []


def test_estimate_rejects_bool_and_zero_values():
    # JSON booleans / zero / negatives must not poison the estimate.
    raw = '{"tokens_est": true, "minutes_est": 0, "steps": ["x"]}'
    est = estimate("build a tool", lambda _p: raw)
    # tokens true -> rejected -> default; minutes 0 -> rejected -> default.
    assert est["tokens_est"] == DEFAULT_TOKENS_EST
    assert est["minutes_est"] == DEFAULT_MINUTES_EST
    assert est["steps"] == ["x"]


def test_estimate_invocation_is_offline_and_injected():
    # Proves run_fn is the only path to a model: we record the call and assert
    # nothing else happened. No claude subprocess, no network.
    calls: list[str] = []

    def run_fn(prompt: str) -> str:
        calls.append(prompt)
        return '{"tokens_est": 1000, "minutes_est": 1, "steps": []}'

    estimate("build a thing", run_fn)
    assert len(calls) == 1
    assert calls[0] == build_estimate_prompt("build a thing")


# ---------------------------------------------------------------------------
# speak_line()
# ---------------------------------------------------------------------------


def test_speak_line_canonical():
    est = {"tokens_est": 80000, "minutes_est": 12, "steps": []}
    assert speak_line(est, "Tony") == (
        "Roughly 80k tokens, about 12 minutes, Tony. Starting now."
    )


def test_speak_line_singular_minute():
    est = {"tokens_est": 5000, "minutes_est": 1, "steps": []}
    line = speak_line(est, "Tony")
    assert "1 minute," in line
    assert "1 minutes" not in line


def test_speak_line_millions_and_fraction():
    est = {"tokens_est": 1_500_000, "minutes_est": 2.5, "steps": []}
    line = speak_line(est, "Tony")
    assert "1.5 million tokens" in line
    assert "2.5 minutes" in line


def test_speak_line_no_name_omits_address():
    est = {"tokens_est": 40000, "minutes_est": 6, "steps": []}
    line = speak_line(est, "")
    assert line == "Roughly 40k tokens, about 6 minutes. Starting now."


def test_speak_line_has_no_markdown():
    est = {"tokens_est": 80000, "minutes_est": 12, "steps": ["a", "b"]}
    line = speak_line(est, "Tony")
    for token in ("*", "#", "`", "\n", "- "):
        assert token not in line


def test_speak_line_handles_missing_fields():
    # A malformed est dict must not crash the spoken line.
    line = speak_line({}, "Tony")
    assert "Tony" in line
    assert "Starting now." in line


# ---------------------------------------------------------------------------
# build_estimate_prompt()
# ---------------------------------------------------------------------------


def test_build_estimate_prompt_embeds_task_and_demands_json():
    prompt = build_estimate_prompt("  build a CLI  ")
    assert "build a CLI" in prompt
    assert "JSON" in prompt
    assert "tokens_est" in prompt
    assert "minutes_est" in prompt
    assert "steps" in prompt


def test_module_exports_public_api():
    for name in ("is_project_intent", "estimate", "speak_line"):
        assert hasattr(estimator, name)
        assert callable(getattr(estimator, name))
