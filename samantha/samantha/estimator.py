"""Pre-task estimation for build / project intents (spec 4.7).

When the user asks Samantha to *build* something rather than just chat,
she states an upfront estimate before diving in -- honoring the standing
"estimate-first" rule. This module owns three pieces:

* :func:`is_project_intent` -- a fast, keyword-driven classifier that decides
  whether a message is a request to build/create something, while avoiding
  false positives on casual conversation.
* :func:`estimate` -- runs one cheap model call (injected via ``run_fn`` so it
  is fully testable offline; **no real claude call lives here**) and parses a
  ``{tokens_est, minutes_est, steps}`` estimate out of the response, robustly,
  with a heuristic fallback when the output is not clean JSON.
* :func:`speak_line` -- turns an estimate into a short spoken line addressed to
  the user by name.

Nothing in this module touches the network, the mic, or the claude CLI.
The model call is always supplied by the caller as ``run_fn``.
"""

from __future__ import annotations

import json
import re
from typing import Callable

__all__ = [
    "is_project_intent",
    "estimate",
    "speak_line",
    "build_estimate_prompt",
    "DEFAULT_TOKENS_EST",
    "DEFAULT_MINUTES_EST",
]

# A model-call function: takes a prompt string, returns the model's raw text.
RunFn = Callable[[str], str]

# Conservative defaults used when the model gives us nothing usable at all.
DEFAULT_TOKENS_EST = 40_000
DEFAULT_MINUTES_EST = 6.0

# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

# Strong build verbs: a single, clear occurrence is enough to flag the message.
# Each pattern is a word-boundaried regex so "rebuild" / "created" still match
# but "increate" or "scaffolding-talk" inside another word generally do not.
_STRONG_INTENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bbuild(s|ing|t)?\b"),
    re.compile(r"\bre-?build(s|ing|t)?\b"),
    re.compile(r"\bcreate(s|d|ing)?\b"),
    re.compile(r"\bimplement(s|ed|ing|ation)?\b"),
    re.compile(r"\bdeploy(s|ed|ing|ment)?\b"),
    re.compile(r"\brefactor(s|ed|ing)?\b"),
    re.compile(r"\bscaffold(s|ed|ing)?\b"),
    re.compile(r"\bset\s+up\b"),
    re.compile(r"\bsetup\b"),
    re.compile(r"\bspin\s+up\b"),
    re.compile(r"\bgenerate\s+(?:a|an|the|some|me)\b"),
    re.compile(r"\bmake\s+me\s+(?:a|an)\b"),
    re.compile(r"\bwrite\s+(?:me\s+)?(?:a|an|the|some)\s+\w*\s*(?:app|script|tool|cli|function|class|module|program|api|service|bot|page|site|website|component)s?\b"),
    re.compile(r"\bcode\s+(?:me\s+)?(?:a|an|the|up)\b"),
)

# Casual phrases that look superficially build-y but are just chat / questions
# about the topic. If the message is *dominated* by one of these and carries no
# strong, unambiguous build instruction, we treat it as conversation.
_CASUAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bwhat\s+(?:did|do|are|is|was|were)\s+you\b"),
    re.compile(r"\bhow\s+do\s+(?:you|i)\s+feel\b"),
    re.compile(r"\btell\s+me\s+about\b"),
    re.compile(r"\bwhat\s+do\s+you\s+think\b"),
    re.compile(r"\bdid\s+you\s+(?:build|create|make|write)\b"),
    re.compile(r"\bhave\s+you\s+ever\b"),
    re.compile(r"\bdo\s+you\s+(?:like|enjoy|remember|know)\b"),
    re.compile(r"\bmakes?\s+me\s+(?:feel|happy|sad|think|smile|laugh)\b"),
    re.compile(r"\bi\s+(?:want\s+to|wanna|would\s+like\s+to)\s+build\s+(?:a\s+)?(?:relationship|connection|life|future|home|family)\b"),
)


def is_project_intent(text: str) -> bool:
    """Return ``True`` if *text* is a request to build/create something.

    The classifier is intentionally cheap and offline: it scans for strong
    build verbs (``build``, ``create``, ``implement``, ``deploy``, ``refactor``,
    ``scaffold``, ``set up``/``setup``, ``make me a/an``, ``write a/an X
    app/script/tool``, ...) while filtering out casual conversation that merely
    *mentions* those words ("what did you build today?", "that makes me feel...").

    Args:
        text: The raw user utterance.

    Returns:
        Whether Samantha should run the estimate gate before responding.
    """
    if not text or not text.strip():
        return False

    lowered = text.lower()

    matched = any(p.search(lowered) for p in _STRONG_INTENT_PATTERNS)
    if not matched:
        return False

    # We have a build verb. Suppress the obvious conversational uses unless the
    # message also contains a concrete software noun that signals real work.
    if any(p.search(lowered) for p in _CASUAL_PATTERNS):
        if not _has_software_object(lowered):
            return False

    return True


_SOFTWARE_OBJECT = re.compile(
    r"\b(app|application|script|tool|cli|function|class|module|program|"
    r"api|endpoint|service|microservice|bot|page|site|website|component|"
    r"feature|server|database|schema|pipeline|dashboard|widget|library|"
    r"package|repo|repository|project|test|tests|suite|integration)s?\b"
)


def _has_software_object(lowered: str) -> bool:
    """Whether the (already-lowercased) text names a concrete software artifact."""
    return bool(_SOFTWARE_OBJECT.search(lowered))


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------

_ESTIMATE_PROMPT = (
    "You are a build estimator. The user asked an AI coding assistant to do a "
    "task. Estimate the effort and reply with ONLY a JSON object, no prose, no "
    "markdown fences, in exactly this shape:\n"
    '{{"tokens_est": <integer total tokens>, "minutes_est": <number of minutes>, '
    '"steps": ["short step", "short step", ...]}}\n'
    "Keep steps to 3-6 short phrases. Be realistic for a senior engineer using "
    "an AI assistant.\n\n"
    "Task: {task}"
)


def build_estimate_prompt(task: str) -> str:
    """Build the prompt handed to ``run_fn`` for an estimate.

    Exposed so callers can inspect / log exactly what was asked, and so tests
    can assert on it without reaching into private state.
    """
    return _ESTIMATE_PROMPT.format(task=task.strip())


def estimate(task: str, run_fn: RunFn) -> dict:
    """Estimate the effort for *task* using the injected model call *run_fn*.

    ``run_fn`` is a ``Callable[[str], str]`` -- it receives a prompt and returns
    the model's raw text. It is injected so this function is fully testable
    offline; **no real claude call is made here**.

    The model output is parsed robustly: clean JSON first, then JSON embedded in
    surrounding text or markdown fences, and finally a number-scraping heuristic.
    If absolutely nothing usable comes back (including when ``run_fn`` raises),
    conservative defaults are returned so the caller never crashes mid-loop.

    Args:
        task: The user's build request.
        run_fn: Injected model-call function ``(prompt) -> raw_text``.

    Returns:
        A dict ``{"tokens_est": int, "minutes_est": float, "steps": list[str]}``.
        ``tokens_est`` is always a positive int, ``minutes_est`` a positive
        float, and ``steps`` a list of non-empty strings (possibly empty list).
    """
    prompt = build_estimate_prompt(task)

    try:
        raw = run_fn(prompt)
    except Exception:
        raw = ""

    raw = raw or ""

    parsed = _parse_estimate(raw)
    if parsed is not None:
        return parsed

    return _heuristic_estimate(raw, task)


def _parse_estimate(raw: str) -> dict | None:
    """Try to extract a well-formed estimate dict from raw model text.

    Returns a normalized dict, or ``None`` if no JSON object with at least one
    of the expected numeric fields could be recovered.
    """
    for candidate in _iter_json_candidates(raw):
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        tokens = _coerce_int(obj.get("tokens_est"))
        minutes = _coerce_float(obj.get("minutes_est"))
        # Require at least one real numeric signal to trust this as JSON.
        if tokens is None and minutes is None:
            continue
        return _normalize(tokens, minutes, obj.get("steps"))
    return None


def _iter_json_candidates(raw: str):
    """Yield progressively-recovered JSON-object substrings from *raw*.

    Order matters: the cleanest interpretation is tried first.
    1. The whole string, trimmed.
    2. Content inside a ```json ...``` or ``` ... ``` fence.
    3. The first balanced ``{...}`` span found anywhere in the text.
    """
    stripped = raw.strip()
    if stripped:
        yield stripped

    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.DOTALL | re.IGNORECASE)
    if fence:
        inner = fence.group(1).strip()
        if inner:
            yield inner

    span = _first_balanced_object(raw)
    if span is not None:
        yield span


def _first_balanced_object(raw: str) -> str | None:
    """Return the first balanced ``{...}`` substring, respecting strings/escapes."""
    start = raw.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return raw[start : i + 1]
        start = raw.find("{", start + 1)
    return None


def _heuristic_estimate(raw: str, task: str) -> dict:
    """Best-effort estimate when JSON parsing fails entirely.

    Scrapes loose numbers from the model text (e.g. "around 80k tokens, ~12
    minutes") and falls back to size-scaled defaults derived from the task
    itself when even that yields nothing.
    """
    tokens = _scrape_tokens(raw)
    minutes = _scrape_minutes(raw)
    steps = _scrape_steps(raw)

    if tokens is None and minutes is None and not steps:
        # Nothing recoverable from the model -- scale a default off task size so
        # bigger asks read as bigger estimates.
        words = len(task.split())
        tokens = max(DEFAULT_TOKENS_EST, words * 1_500)
        minutes = round(max(DEFAULT_MINUTES_EST, words * 0.4), 1)

    return _normalize(tokens, minutes, steps)


# Matches "80k", "80 k", "80,000", "80000" optionally followed by "tokens".
_TOKEN_NUM = re.compile(
    r"([\d][\d,\.]*)\s*([km])?\s*tokens?",
    re.IGNORECASE,
)
_MINUTES_NUM = re.compile(
    r"([\d][\d\.]*)\s*(?:minutes?|mins?|m\b)",
    re.IGNORECASE,
)
_BARE_NUM = re.compile(r"[\d][\d,\.]*")


def _scrape_tokens(raw: str) -> int | None:
    m = _TOKEN_NUM.search(raw)
    if not m:
        return None
    value = _num(m.group(1))
    if value is None:
        return None
    suffix = (m.group(2) or "").lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return int(round(value))


def _scrape_minutes(raw: str) -> float | None:
    m = _MINUTES_NUM.search(raw)
    if not m:
        return None
    return _num(m.group(1))


def _scrape_steps(raw: str) -> list[str]:
    """Pull bullet / numbered lines out of unstructured model text."""
    steps: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(r"^(?:[-*•]|\d+[\.\)])\s+(.*\S)", stripped)
        if m:
            steps.append(m.group(1).strip())
    return steps


def _num(text: str) -> float | None:
    """Parse a possibly comma-grouped number string to float."""
    cleaned = text.replace(",", "").strip(" .")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


_SCALED_NUM = re.compile(r"^\s*([\d][\d,\.]*)\s*([km])?\s*$", re.IGNORECASE)


def _scaled_num(text: str) -> float | None:
    """Parse a bare number string with an optional ``k``/``m`` suffix.

    Handles values like ``"80k"`` or ``"1.5m"`` that arrive as JSON strings
    rather than as numbers. Returns ``None`` if the string is not a clean
    (optionally suffixed) number.
    """
    m = _SCALED_NUM.match(text)
    if not m:
        return None
    value = _num(m.group(1))
    if value is None:
        return None
    suffix = (m.group(2) or "").lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return value


# ---------------------------------------------------------------------------
# Normalization & speaking
# ---------------------------------------------------------------------------


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):  # bool is an int subclass -- reject it explicitly.
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str):
        n = _scrape_tokens(value)
        if n is not None:
            return n
        scaled = _scaled_num(value)
        return int(round(scaled)) if scaled is not None else None
    return None


def _coerce_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return _num(value)
    return None


def _coerce_steps(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(s).strip() for s in value if str(s).strip()]
    if isinstance(value, str):
        parts = [p.strip() for p in re.split(r"[\n;]+", value)]
        return [p for p in parts if p]
    return []


def _normalize(
    tokens: int | float | None,
    minutes: int | float | None,
    steps: object,
) -> dict:
    """Coerce raw pieces into the canonical, always-valid estimate dict."""
    if tokens is None or tokens <= 0:
        tokens_out = DEFAULT_TOKENS_EST
    else:
        tokens_out = int(round(tokens))

    if minutes is None or minutes <= 0:
        minutes_out = DEFAULT_MINUTES_EST
    else:
        minutes_out = round(float(minutes), 1)

    return {
        "tokens_est": tokens_out,
        "minutes_est": minutes_out,
        "steps": _coerce_steps(steps),
    }


def _humanize_tokens(tokens: int) -> str:
    """Render a token count the way Samantha would say it (e.g. ``80k``)."""
    if tokens >= 1_000_000:
        millions = tokens / 1_000_000
        text = f"{millions:.1f}".rstrip("0").rstrip(".")
        return f"{text} million"
    if tokens >= 1_000:
        thousands = tokens / 1_000
        text = f"{thousands:.0f}" if thousands == int(thousands) else f"{thousands:.1f}"
        return f"{text}k"
    return str(tokens)


def _humanize_minutes(minutes: float) -> str:
    """Render a minute count naturally (``12 minutes`` / ``half a minute``)."""
    if minutes < 1:
        return "under a minute"
    rounded = round(minutes)
    if abs(minutes - rounded) < 0.05:
        unit = "minute" if rounded == 1 else "minutes"
        return f"{rounded} {unit}"
    text = f"{minutes:.1f}".rstrip("0").rstrip(".")
    unit = "minute" if text == "1" else "minutes"
    return f"{text} {unit}"


def speak_line(est: dict, user_name: str) -> str:
    """Compose Samantha's spoken estimate line.

    Example::

        >>> speak_line({"tokens_est": 80000, "minutes_est": 12, "steps": []}, "Tony")
        'Roughly 80k tokens, about 12 minutes, Tony. Starting now.'

    Args:
        est: An estimate dict as returned by :func:`estimate`.
        user_name: The name to address (e.g. ``"Tony"``).

    Returns:
        A short, natural, markdown-free spoken line.
    """
    tokens = _coerce_int(est.get("tokens_est")) or DEFAULT_TOKENS_EST
    minutes = _coerce_float(est.get("minutes_est"))
    if minutes is None or minutes <= 0:
        minutes = DEFAULT_MINUTES_EST

    name = (user_name or "").strip()
    address = f", {name}" if name else ""

    return (
        f"Roughly {_humanize_tokens(tokens)} tokens, "
        f"about {_humanize_minutes(minutes)}{address}. Starting now."
    )
