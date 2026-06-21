"""Goal tracking for Samantha.

A single active goal is persisted as JSON (default ``~/.samantha/goal.json``)
and injected into the persona every turn so Claude stays pointed at what Tony
is trying to ship. Drives the autonomous-loop stop condition: when the goal is
marked done, the loop knows it's finished.

The persistence path is a parameter on every operation (defaulting to
``DEFAULT_GOAL_PATH``) so tests -- and any future multi-goal use -- can point at
an isolated file. Everything here is offline: no network, no claude, no mic.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_GOAL_PATH = Path.home() / ".samantha" / "goal.json"

STATUS_ACTIVE = "active"
STATUS_DONE = "done"


@dataclass
class Goal:
    """A single tracked goal.

    Attributes:
        text: What Tony is trying to achieve.
        created: ISO-8601 timestamp of when the goal was set.
        status: Either ``"active"`` or ``"done"``.
        notes: Free-form progress notes appended over the goal's life.
    """

    text: str
    created: str = ""
    status: str = STATUS_ACTIVE
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.created:
            self.created = _now()
        if self.status not in (STATUS_ACTIVE, STATUS_DONE):
            self.status = STATUS_ACTIVE

    @property
    def is_done(self) -> bool:
        """True when the goal has been marked done."""
        return self.status == STATUS_DONE

    def to_dict(self) -> dict:
        """Serialize to a plain dict for JSON persistence."""
        return {
            "text": self.text,
            "created": self.created,
            "status": self.status,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Goal":
        """Reconstruct a Goal from persisted JSON, tolerating partial data."""
        notes = data.get("notes") or []
        if not isinstance(notes, list):
            notes = [str(notes)]
        return cls(
            text=str(data.get("text", "")),
            created=str(data.get("created", "")),
            status=str(data.get("status", STATUS_ACTIVE)),
            notes=[str(n) for n in notes],
        )


def _now() -> str:
    """Current local time as an ISO-8601-ish timestamp."""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _resolve(path: Optional[Path]) -> Path:
    """Coerce an optional path argument to a concrete Path."""
    return Path(path) if path is not None else DEFAULT_GOAL_PATH


def set_goal(text: str, path: Optional[Path] = None) -> Goal:
    """Set (or replace) the active goal and persist it.

    Args:
        text: The goal text. Surrounding whitespace is stripped.
        path: Where to persist. Defaults to ``DEFAULT_GOAL_PATH``.

    Returns:
        The newly created :class:`Goal`.

    Raises:
        ValueError: If ``text`` is empty or whitespace-only.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Goal text cannot be empty.")
    goal = Goal(text=text)
    _write(goal, _resolve(path))
    return goal


def get_goal(path: Optional[Path] = None) -> Optional[Goal]:
    """Load the persisted goal, or ``None`` if no goal is set.

    Returns ``None`` rather than raising on a missing or corrupt file, so
    callers can inject "no goal" cleanly each turn.
    """
    target = _resolve(path)
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or not data.get("text"):
        return None
    return Goal.from_dict(data)


def clear_goal(path: Optional[Path] = None) -> bool:
    """Remove the persisted goal entirely.

    Returns:
        True if a goal file existed and was removed, False otherwise.
    """
    target = _resolve(path)
    if not target.exists():
        return False
    try:
        target.unlink()
        return True
    except OSError:
        return False


def add_note(note: str, path: Optional[Path] = None) -> Optional[Goal]:
    """Append a progress note to the active goal and persist it.

    Args:
        note: The note text. Surrounding whitespace is stripped.
        path: Where the goal is persisted.

    Returns:
        The updated :class:`Goal`, or ``None`` if no goal is set.

    Raises:
        ValueError: If ``note`` is empty or whitespace-only.
    """
    note = (note or "").strip()
    if not note:
        raise ValueError("Note text cannot be empty.")
    target = _resolve(path)
    goal = get_goal(target)
    if goal is None:
        return None
    goal.notes.append(note)
    _write(goal, target)
    return goal


def mark_done(path: Optional[Path] = None) -> Optional[Goal]:
    """Mark the active goal done and persist it.

    Returns:
        The updated :class:`Goal`, or ``None`` if no goal is set.
    """
    target = _resolve(path)
    goal = get_goal(target)
    if goal is None:
        return None
    goal.status = STATUS_DONE
    _write(goal, target)
    return goal


def inject_text(goal: Optional[Goal]) -> str:
    """Render the goal as a persona-injectable string.

    Returns an empty string when there's no active goal, so callers can
    unconditionally splice the result into the prompt without a branch.

    Args:
        goal: The goal to render (typically from :func:`get_goal`).

    Returns:
        A short block like ``"Current goal: ship X\\nProgress notes:\\n- ..."``
        for an active goal, or ``""`` when ``goal`` is None or already done.
    """
    if goal is None or goal.is_done:
        return ""
    lines = [f"Current goal: {goal.text}"]
    if goal.notes:
        lines.append("Progress notes:")
        lines.extend(f"- {note}" for note in goal.notes)
    return "\n".join(lines)


def _write(goal: Goal, path: Path) -> None:
    """Atomically persist a goal to ``path``, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(goal.to_dict(), indent=2)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload)
    tmp.replace(path)
