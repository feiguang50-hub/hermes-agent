"""Regression tests for curator skill activity timestamps."""

import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _write_skill(skills_dir: Path, name: str) -> None:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\n# {name}\n",
        encoding="utf-8",
    )


@pytest.fixture
def curator_modules(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    import tools.skill_usage as skill_usage
    import agent.curator as curator

    importlib.reload(skill_usage)
    importlib.reload(curator)
    return home, skill_usage, curator


def test_recent_view_activity_prevents_false_stale_transition(curator_modules, monkeypatch):
    home, skill_usage, curator = curator_modules
    skills_dir = home / "skills"
    _write_skill(skills_dir, "recently-viewed")

    now = datetime(2026, 4, 30, tzinfo=timezone.utc)
    created_at = (now - timedelta(days=60)).isoformat()
    last_viewed_at = (now - timedelta(days=1)).isoformat()
    skill_usage.save_usage({
        "recently-viewed": {
            "created_at": created_at,
            "last_viewed_at": last_viewed_at,
            "view_count": 1,
            "state": "active",
        }
    })
    monkeypatch.setattr(curator, "get_stale_after_days", lambda: 30)
    monkeypatch.setattr(curator, "get_archive_after_days", lambda: 90)

    counts = curator.apply_automatic_transitions(now=now)

    assert counts["marked_stale"] == 0
    assert skill_usage.get_record("recently-viewed")["state"] == "active"


def test_prune_does_not_archive_split_or_deprecated(curator_modules, monkeypatch):
    """R7: split / deprecated are deliberate lifecycle decisions. The
    deterministic inactivity prune must NOT archive them even when they are
    idle well past the archive threshold — archiving would overwrite the
    lifecycle state and destroy the split_into / replaced_by pointer.

    A same-age plain 'active' skill IS archived in the same pass, proving the
    prune is live and the split/deprecated skills are spared by the guard, not
    merely absent from the report.
    """
    home, skill_usage, curator = curator_modules
    skills_dir = home / "skills"
    for n in ("dep-skill", "split-skill", "old-active"):
        _write_skill(skills_dir, n)

    now = datetime(2026, 4, 30, tzinfo=timezone.utc)
    old = (now - timedelta(days=200)).isoformat()  # well past a 90d archive cutoff
    skill_usage.save_usage({
        "dep-skill": {
            "created_by": "agent",
            "created_at": old, "last_used_at": old, "use_count": 3,
            "state": "deprecated", "replaced_by": "umbrella-skill",
        },
        "split-skill": {
            "created_by": "agent",
            "created_at": old, "last_used_at": old, "use_count": 3,
            "state": "split", "split_into": ["a-skill", "b-skill"],
        },
        "old-active": {
            "created_by": "agent",
            "created_at": old, "last_used_at": old, "use_count": 3,
            "state": "active",
        },
    })
    monkeypatch.setattr(curator, "get_stale_after_days", lambda: 30)
    monkeypatch.setattr(curator, "get_archive_after_days", lambda: 90)

    counts = curator.apply_automatic_transitions(now=now)

    # split / deprecated states preserved, pointers intact
    dep = skill_usage.get_record("dep-skill")
    spl = skill_usage.get_record("split-skill")
    assert dep["state"] == "deprecated"
    assert dep.get("replaced_by") == "umbrella-skill"
    assert spl["state"] == "split"
    assert spl.get("split_into") == ["a-skill", "b-skill"]

    # control: an equally-old plain 'active' skill IS archived — proves the
    # prune actually ran on same-age skills this pass.
    assert skill_usage.get_record("old-active")["state"] == "archived"
    assert counts["archived"] == 1
