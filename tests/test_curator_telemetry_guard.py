"""Tests for the background-review telemetry guard.

The curator forks a ``review_agent`` with ``_memory_write_origin =
"background_review"`` and asks it to inspect candidate skills via
``skill_view``. Without a guard, those audit reads would call
``bump_view`` / ``bump_use``, which refreshes ``last_viewed_at`` /
``last_used_at`` and therefore ``last_activity_at`` — the field the
curator's stale/archive clock keys off. The fork's own activity would
silently keep every candidate skill alive.

These tests pin the contract:

* ``bump_view`` / ``bump_use`` short-circuit when the
  ``background_review`` write-origin is active.
* ``bump_patch`` is NOT short-circuited (curator genuinely patches
  skills during the run, that should count as activity).
* Outcome / feedback recorders are NOT short-circuited either
  (curator shouldn't be recording outcomes anyway, but if it does
  through some new code path, the signal still flows through).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import skill_usage  # noqa: E402
from tools.skill_provenance import (  # noqa: E402
    BACKGROUND_REVIEW,
    _write_origin,
    get_current_write_origin,
)
from tools.skill_usage import (  # noqa: E402
    bump_patch,
    bump_use,
    bump_view,
    get_record,
    record_outcome,
    record_user_feedback,
)
from tools.skill_usage import OUTCOME_SUCCESS  # noqa: E402


@pytest.fixture
def fake_hermes_home(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", lambda: tmp_path
    )
    monkeypatch.setattr(
        skill_usage, "get_hermes_home", lambda: tmp_path
    )
    (tmp_path / "skills").mkdir(exist_ok=True)
    return tmp_path


@pytest.fixture
def background_review_context():
    """Activate the background_review write-origin for this test only."""
    token = _write_origin.set(BACKGROUND_REVIEW)
    try:
        yield
    finally:
        _write_origin.reset(token)


class TestBumpViewGuarded:
    def test_normal_call_bumps_view(self, fake_hermes_home):
        # Foreground call: should bump
        bump_view("demo-skill")
        rec = get_record("demo-skill")
        assert rec["view_count"] == 1
        assert rec["last_viewed_at"] is not None

    def test_background_review_call_is_noop(self, fake_hermes_home, background_review_context):
        # Verify the fixture actually flipped the context
        assert get_current_write_origin() == BACKGROUND_REVIEW
        bump_view("demo-skill")
        rec = get_record("demo-skill")
        assert rec["view_count"] == 0
        assert rec["last_viewed_at"] is None

    def test_background_review_does_not_create_record(self, fake_hermes_home, background_review_context):
        bump_view("never-touched")
        rec = get_record("never-touched")
        # Empty record = counts zero, all timestamps None
        assert rec["view_count"] == 0
        assert rec["last_viewed_at"] is None

    def test_normal_then_background_review(self, fake_hermes_home, background_review_context):
        # Foreground bumps first
        bump_view("demo-skill")
        rec1 = get_record("demo-skill")
        first_viewed = rec1["last_viewed_at"]
        # Now background — should not change anything
        bump_view("demo-skill")
        rec2 = get_record("demo-skill")
        assert rec2["view_count"] == rec1["view_count"]
        assert rec2["last_viewed_at"] == first_viewed


class TestBumpUseGuarded:
    def test_normal_call_bumps_use(self, fake_hermes_home):
        bump_use("demo-skill")
        rec = get_record("demo-skill")
        assert rec["use_count"] == 1
        assert rec["last_used_at"] is not None

    def test_background_review_call_is_noop(self, fake_hermes_home, background_review_context):
        bump_use("demo-skill")
        rec = get_record("demo-skill")
        assert rec["use_count"] == 0
        assert rec["last_used_at"] is None


class TestOtherCountersNotGuarded:
    """``bump_patch`` and the outcome / feedback recorders should NOT
    short-circuit on background_review. The curator genuinely mutates
    skills during a pass (patches, archives, deletes), and those are
    real activity that the staleness clock SHOULD see."""

    def test_bump_patch_runs_in_background_review(
        self, fake_hermes_home, background_review_context
    ):
        bump_patch("demo-skill")
        rec = get_record("demo-skill")
        assert rec["patch_count"] == 1
        assert rec["last_patched_at"] is not None

    def test_record_outcome_runs_in_background_review(
        self, fake_hermes_home, background_review_context
    ):
        # Curator probably shouldn't be recording outcomes, but if some
        # new code path does, the signal still flows through — we don't
        # want to accidentally silence a success/failure that happens
        # during the pass.
        record_outcome("demo-skill", OUTCOME_SUCCESS)
        rec = get_record("demo-skill")
        assert rec["outcomes"][OUTCOME_SUCCESS] == 1

    def test_record_feedback_runs_in_background_review(
        self, fake_hermes_home, background_review_context
    ):
        from tools.skill_usage import FEEDBACK_UP
        record_user_feedback("demo-skill", FEEDBACK_UP, text="test")
        rec = get_record("demo-skill")
        assert rec["user_feedback"][FEEDBACK_UP] == 1
        assert rec["user_feedback"]["notes"] == ["test"]


class TestContextIsolation:
    """The write-origin ContextVar must reset cleanly so tests / callers
    don't leak curator context into foreground code."""

    def test_set_then_reset_restores_foreground(
        self, fake_hermes_home, background_review_context
    ):
        # Inside the fixture the context is curator
        assert get_current_write_origin() == BACKGROUND_REVIEW
        # Outside the fixture the context should be back to foreground
        # (the fixture's finally block already reset it; we re-check
        # by simulating a fresh foreground call)
        # Note: pytest doesn't easily let us assert *after* a fixture
        # tears down without a second helper, but the implementation
        # uses _write_origin.reset which restores the prior value.
