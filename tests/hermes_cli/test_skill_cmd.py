"""Tests for ``hermes skill`` (singular) CLI — currently only ``score``.

Pins the F (observability dashboard) work item's second half. The score
command calls ``agent.skill_scoring.compute_skill_score(name)`` and prints
the result. It is strictly read-only.
"""

from __future__ import annotations

import argparse
import importlib
import json as _json
import sys

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Redirect HERMES_HOME to a tmp dir with a skills/ subdir.

    Monkeypatches ``hermes_constants.get_hermes_home`` (the canonical
    resolver) AND ``tools.skill_usage.get_hermes_home`` (the module-local
    re-import used by callers). Mirrors the ``fake_hermes_home`` pattern in
    ``tests/test_skill_scoring.py``.
    """
    from hermes_constants import get_hermes_home
    import tools.skill_usage as skill_usage

    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(skill_usage, "get_hermes_home", lambda: tmp_path)
    (tmp_path / "skills").mkdir(exist_ok=True)
    return tmp_path


def _write_usage(hermes_home: "Path", records: dict) -> None:
    """Write a ``.usage.json`` sidecar at <hermes_home>/skills/.usage.json."""
    path = hermes_home / "skills" / ".usage.json"
    path.write_text(_json.dumps(records), encoding="utf-8")


def _seed_record(
    *,
    created_by="agent",
    use_count=0,
    view_count=0,
    successes=0,
    failures=0,
    ups=0,
    downs=0,
    last_used_at=None,
    last_rating=None,
):
    """Build a minimal usage record that ``compute_skill_score`` accepts."""
    return {
        "created_by": created_by,
        "use_count": use_count,
        "view_count": view_count,
        "last_used_at": last_used_at,
        "last_viewed_at": None,
        "patch_count": 0,
        "last_patched_at": None,
        "created_at": "2026-06-01T00:00:00+00:00",
        "state": "active",
        "pinned": False,
        "archived_at": None,
        "outcomes": {
            "success": successes,
            "failure": failures,
            "corrected": 0,
            "abandoned": 0,
            "unknown": 0,
            "last_outcome": "success" if successes else ("failure" if failures else None),
            "last_outcome_at": last_used_at,
            "last_outcome_source": "user",
        },
        "user_feedback": {
            "up": ups,
            "down": downs,
            "last_rating": last_rating,
            "last_rating_at": last_used_at if last_rating else None,
        },
        "split_into": [],
        "replaced_by": None,
    }


def test_cmd_skill_score_table(hermes_home, capsys):
    """Score table prints the score + components + last_outcome/last_rating."""
    import hermes_cli.main as main

    _write_usage(hermes_home, {
        "demo-skill": _seed_record(
            use_count=5, successes=4, failures=1, ups=3, downs=0,
            last_used_at="2026-07-15T10:00:00+00:00", last_rating="up",
        ),
    })

    class _A:
        skill_action = "score"
        name = "demo-skill"
        json = False

    rc = main.cmd_skill(_A())
    out = capsys.readouterr().out
    assert rc == 0
    assert "skill score: demo-skill" in out
    assert "score:" in out
    assert "success_rate:" in out
    assert "feedback_score:" in out
    assert "recency_decay:" in out
    assert "confidence:" in out
    assert "sample_size:" in out
    assert "feedback_total:" in out
    assert "last_outcome:" in out
    assert "last_rating:" in out


def test_cmd_skill_score_json_emits_raw_dict(hermes_home, capsys):
    """--json emits the raw compute_skill_score dict."""
    import hermes_cli.main as main

    _write_usage(hermes_home, {
        "demo-skill": _seed_record(
            use_count=2, successes=2, ups=1, last_used_at="2026-07-15T10:00:00+00:00",
        ),
    })

    class _A:
        skill_action = "score"
        name = "demo-skill"
        json = True

    rc = main.cmd_skill(_A())
    out = capsys.readouterr().out
    assert rc == 0
    parsed = _json.loads(out)
    assert parsed["skill"] == "demo-skill"
    assert isinstance(parsed["score"], float)
    assert "components" in parsed
    assert "weights" in parsed


def test_cmd_skill_score_unknown_skill_warns_and_returns_zero(hermes_home, capsys):
    """A skill with no record returns score 0.0 and a friendly hint."""
    import hermes_cli.main as main

    _write_usage(hermes_home, {
        "never-seen": _seed_record(created_by=None),
    })

    class _A:
        skill_action = "score"
        name = "never-seen"
        json = False

    rc = main.cmd_skill(_A())
    out = capsys.readouterr().out
    assert rc == 0
    assert "score:             0.0000" in out
    assert "no usage record on file" in out


def test_cmd_skill_score_does_not_write_to_usage(hermes_home, capsys):
    """Read-only contract: invoking score must not change .usage.json."""
    import hermes_cli.main as main

    records = {
        "demo-skill": _seed_record(
            use_count=3, successes=2, failures=1, last_used_at="2026-07-15T10:00:00+00:00",
        ),
    }
    _write_usage(hermes_home, records)
    usage_path = hermes_home / "skills" / ".usage.json"
    before = usage_path.read_text(encoding="utf-8")

    class _A:
        skill_action = "score"
        name = "demo-skill"
        json = False

    main.cmd_skill(_A())

    after = usage_path.read_text(encoding="utf-8")
    assert before == after, "score command must not modify .usage.json"


def test_cmd_skill_no_subcommand_returns_help_and_exit_1(capsys):
    """No subcommand → print help and return 1 so users discover score."""
    import hermes_cli.main as main

    class _A:
        skill_action = None
        name = None
        json = False

    rc = main.cmd_skill(_A())
    out = capsys.readouterr().out
    assert rc == 1
    assert "usage:" in out.lower()
    assert "score" in out


def test_build_skill_parser_registers_score_subcommand():
    """build_skill_parser exposes a ``score`` subcommand with the right args."""
    from hermes_cli.subcommands.skill import build_skill_parser

    captured = {}

    def fake_handler(args):
        captured["called"] = True

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_skill_parser(sub, cmd_skill=fake_handler)

    ns = parser.parse_args(["skill", "score", "demo-skill"])
    assert ns.command == "skill"
    assert ns.skill_action == "score"
    assert ns.name == "demo-skill"
    assert ns.func is fake_handler
    assert captured.get("called") is None  # parse_args should not invoke func

    # --json should be parsed and default to False.
    ns2 = parser.parse_args(["skill", "score", "demo-skill"])
    assert ns2.json is False

    ns3 = parser.parse_args(["skill", "score", "demo-skill", "--json"])
    assert ns3.json is True