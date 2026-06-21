"""Offline unit tests for samantha.goal.

Every test points the persistence layer at a tmp path -- no real
``~/.samantha/goal.json`` is touched, and nothing here hits the network,
claude, or the mic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from samantha import goal as goal_mod
from samantha.goal import (
    STATUS_ACTIVE,
    STATUS_DONE,
    Goal,
    add_note,
    clear_goal,
    get_goal,
    inject_text,
    mark_done,
    set_goal,
)


@pytest.fixture()
def goal_path(tmp_path: Path) -> Path:
    """An isolated goal.json path inside a tmp dir (parent not yet created)."""
    return tmp_path / "nested" / "goal.json"


# --------------------------------------------------------------------------- #
# Dataclass behavior
# --------------------------------------------------------------------------- #


def test_goal_defaults_fill_created_and_status() -> None:
    g = Goal(text="ship the loop")
    assert g.text == "ship the loop"
    assert g.created  # auto-populated timestamp
    assert g.status == STATUS_ACTIVE
    assert g.notes == []
    assert g.is_done is False


def test_goal_invalid_status_coerced_to_active() -> None:
    g = Goal(text="x", status="bogus")
    assert g.status == STATUS_ACTIVE


def test_goal_notes_are_independent_between_instances() -> None:
    a = Goal(text="a")
    b = Goal(text="b")
    a.notes.append("only-a")
    assert b.notes == []  # field(default_factory=list), not a shared default


def test_to_dict_from_dict_roundtrip() -> None:
    g = Goal(text="t", created="2026-06-20T10:00:00", status=STATUS_DONE, notes=["n1", "n2"])
    restored = Goal.from_dict(g.to_dict())
    assert restored == g


def test_from_dict_tolerates_missing_and_scalar_notes() -> None:
    g = Goal.from_dict({"text": "t", "notes": "single"})
    assert g.notes == ["single"]
    g2 = Goal.from_dict({"text": "t"})
    assert g2.notes == []


# --------------------------------------------------------------------------- #
# set / get / clear
# --------------------------------------------------------------------------- #


def test_get_goal_none_when_no_file(goal_path: Path) -> None:
    assert get_goal(goal_path) is None


def test_set_goal_persists_and_creates_parent_dirs(goal_path: Path) -> None:
    assert not goal_path.parent.exists()
    g = set_goal("  ship X  ", goal_path)
    assert g.text == "ship X"  # stripped
    assert goal_path.exists()
    assert goal_path.parent.exists()


def test_set_get_roundtrip_with_tmp_path(goal_path: Path) -> None:
    set_goal("ship the ultimate loop", goal_path)
    loaded = get_goal(goal_path)
    assert loaded is not None
    assert loaded.text == "ship the ultimate loop"
    assert loaded.status == STATUS_ACTIVE
    assert loaded.notes == []


def test_persisted_file_is_valid_json_with_expected_shape(goal_path: Path) -> None:
    set_goal("ship X", goal_path)
    data = json.loads(goal_path.read_text())
    assert set(data) == {"text", "created", "status", "notes"}
    assert data["text"] == "ship X"
    assert data["status"] == STATUS_ACTIVE
    assert data["notes"] == []


def test_set_goal_replaces_existing(goal_path: Path) -> None:
    set_goal("old goal", goal_path)
    add_note("progress on old", goal_path)
    set_goal("new goal", goal_path)
    loaded = get_goal(goal_path)
    assert loaded is not None
    assert loaded.text == "new goal"
    assert loaded.notes == []  # fresh goal, old notes gone


def test_set_goal_rejects_empty(goal_path: Path) -> None:
    with pytest.raises(ValueError):
        set_goal("   ", goal_path)
    assert not goal_path.exists()


def test_clear_goal(goal_path: Path) -> None:
    set_goal("ship X", goal_path)
    assert clear_goal(goal_path) is True
    assert get_goal(goal_path) is None
    assert clear_goal(goal_path) is False  # already gone


def test_get_goal_none_on_corrupt_file(goal_path: Path) -> None:
    goal_path.parent.mkdir(parents=True, exist_ok=True)
    goal_path.write_text("{ this is not valid json ")
    assert get_goal(goal_path) is None


def test_get_goal_none_when_text_missing(goal_path: Path) -> None:
    goal_path.parent.mkdir(parents=True, exist_ok=True)
    goal_path.write_text(json.dumps({"status": "active", "notes": []}))
    assert get_goal(goal_path) is None


# --------------------------------------------------------------------------- #
# add_note / mark_done
# --------------------------------------------------------------------------- #


def test_add_note_appends_and_persists(goal_path: Path) -> None:
    set_goal("ship X", goal_path)
    add_note("first step done", goal_path)
    add_note("second step done", goal_path)
    loaded = get_goal(goal_path)
    assert loaded is not None
    assert loaded.notes == ["first step done", "second step done"]


def test_add_note_strips_whitespace(goal_path: Path) -> None:
    set_goal("ship X", goal_path)
    g = add_note("  trimmed note  ", goal_path)
    assert g is not None
    assert g.notes == ["trimmed note"]


def test_add_note_returns_none_when_no_goal(goal_path: Path) -> None:
    assert add_note("orphan note", goal_path) is None


def test_add_note_rejects_empty(goal_path: Path) -> None:
    set_goal("ship X", goal_path)
    with pytest.raises(ValueError):
        add_note("  ", goal_path)


def test_mark_done(goal_path: Path) -> None:
    set_goal("ship X", goal_path)
    g = mark_done(goal_path)
    assert g is not None
    assert g.status == STATUS_DONE
    assert g.is_done is True
    loaded = get_goal(goal_path)
    assert loaded is not None
    assert loaded.status == STATUS_DONE


def test_mark_done_preserves_notes(goal_path: Path) -> None:
    set_goal("ship X", goal_path)
    add_note("did the thing", goal_path)
    mark_done(goal_path)
    loaded = get_goal(goal_path)
    assert loaded is not None
    assert loaded.notes == ["did the thing"]
    assert loaded.status == STATUS_DONE


def test_mark_done_returns_none_when_no_goal(goal_path: Path) -> None:
    assert mark_done(goal_path) is None


# --------------------------------------------------------------------------- #
# inject_text
# --------------------------------------------------------------------------- #


def test_inject_text_none_goal_is_empty() -> None:
    assert inject_text(None) == ""


def test_inject_text_done_goal_is_empty() -> None:
    g = Goal(text="ship X", status=STATUS_DONE)
    assert inject_text(g) == ""


def test_inject_text_active_goal_no_notes() -> None:
    g = Goal(text="ship the loop")
    assert inject_text(g) == "Current goal: ship the loop"


def test_inject_text_active_goal_with_notes() -> None:
    g = Goal(text="ship the loop", notes=["wave 1 done", "wave 2 in flight"])
    out = inject_text(g)
    assert out == (
        "Current goal: ship the loop\n"
        "Progress notes:\n"
        "- wave 1 done\n"
        "- wave 2 in flight"
    )


def test_inject_text_roundtrips_from_persistence(goal_path: Path) -> None:
    set_goal("ship the loop", goal_path)
    add_note("kicked off", goal_path)
    out = inject_text(get_goal(goal_path))
    assert "Current goal: ship the loop" in out
    assert "- kicked off" in out


# --------------------------------------------------------------------------- #
# default path wiring (no real file written)
# --------------------------------------------------------------------------- #


def test_default_path_used_when_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = tmp_path / "default" / "goal.json"
    monkeypatch.setattr(goal_mod, "DEFAULT_GOAL_PATH", fake)
    set_goal("via default path")
    assert fake.exists()
    loaded = get_goal()
    assert loaded is not None
    assert loaded.text == "via default path"
    assert clear_goal() is True
