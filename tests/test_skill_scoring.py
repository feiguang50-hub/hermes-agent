"""Tests for Task A: outcome + user_feedback schema and skill scoring.

Covers the new fields in ``tools/skill_usage.py`` and the score blend in
``agent/skill_scoring.py``. The scoring function is the consumer of
these fields; if the schema drifts the tests here will catch it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import skill_usage  # noqa: E402
from tools.skill_usage import (  # noqa: E402
    FEEDBACK_DOWN,
    FEEDBACK_UP,
    OUTCOME_ABANDONED,
    OUTCOME_CORRECTED,
    OUTCOME_FAILURE,
    OUTCOME_SUCCESS,
    OUTCOME_UNKNOWN,
    OUTCOME_SOURCE_AUTO,
    OUTCOME_SOURCE_HEURISTIC,
    OUTCOME_SOURCE_USER,
    get_record,
    record_outcome,
    record_user_feedback,
)
from agent import skill_scoring  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_hermes_home(tmp_path, monkeypatch):
    """Redirect ~/.hermes to a tmp dir so we don't pollute the real one."""
    from hermes_constants import get_hermes_home
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", lambda: tmp_path
    )
    # also patch the module-level reference inside skill_usage
    monkeypatch.setattr(skill_usage, "get_hermes_home", lambda: tmp_path)
    # Ensure skills/ exists (skill_usage writes there)
    (tmp_path / "skills").mkdir(exist_ok=True)
    return tmp_path


# ---------------------------------------------------------------------------
# record_outcome
# ---------------------------------------------------------------------------

class TestRecordOutcome:
    def test_records_success_increments_counter(self, fake_hermes_home):
        record_outcome("demo-skill", OUTCOME_SUCCESS, OUTCOME_SOURCE_AUTO)
        rec = get_record("demo-skill")
        assert rec["outcomes"][OUTCOME_SUCCESS] == 1
        assert rec["outcomes"]["last_outcome"] == OUTCOME_SUCCESS
        assert rec["outcomes"]["last_outcome_source"] == OUTCOME_SOURCE_AUTO
        assert rec["outcomes"]["last_outcome_at"] is not None

    def test_records_failure(self, fake_hermes_home):
        record_outcome("demo-skill", OUTCOME_FAILURE)
        rec = get_record("demo-skill")
        assert rec["outcomes"][OUTCOME_FAILURE] == 1
        assert rec["outcomes"]["last_outcome"] == OUTCOME_FAILURE

    def test_invalid_outcome_is_silently_dropped(self, fake_hermes_home):
        record_outcome("demo-skill", "bogus_outcome")
        # Record is created with all-zero counters; bogus not stored.
        rec = get_record("demo-skill")
        assert rec["outcomes"][OUTCOME_SUCCESS] == 0
        assert rec["outcomes"]["last_outcome"] is None

    def test_invalid_source_falls_back_to_heuristic(self, fake_hermes_home):
        record_outcome("demo-skill", OUTCOME_SUCCESS, source="bogus_source")
        rec = get_record("demo-skill")
        assert rec["outcomes"]["last_outcome_source"] == OUTCOME_SOURCE_HEURISTIC

    def test_backfills_missing_outcome_counters(self, fake_hermes_home):
        """An old record without the outcomes dict should be backfilled on
        read, not crash on record."""
        # Manually write a record WITHOUT outcomes
        path = fake_hermes_home / "skills" / ".usage.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({
            "old-skill": {"use_count": 5, "view_count": 1},
        }), encoding="utf-8")
        record_outcome("old-skill", OUTCOME_SUCCESS)
        rec = get_record("old-skill")
        assert rec["outcomes"][OUTCOME_SUCCESS] == 1
        assert rec["outcomes"][OUTCOME_FAILURE] == 0  # backfilled
        assert rec["use_count"] == 5  # untouched

    def test_empty_skill_name_is_noop(self, fake_hermes_home):
        record_outcome("", OUTCOME_SUCCESS)
        # No record created
        path = fake_hermes_home / "skills" / ".usage.json"
        assert not path.exists()

    def test_corrected_outcome_supported(self, fake_hermes_home):
        record_outcome("demo-skill", OUTCOME_CORRECTED, OUTCOME_SOURCE_USER)
        rec = get_record("demo-skill")
        assert rec["outcomes"][OUTCOME_CORRECTED] == 1
        assert rec["outcomes"]["last_outcome_source"] == OUTCOME_SOURCE_USER


# ---------------------------------------------------------------------------
# record_user_feedback
# ---------------------------------------------------------------------------

class TestRecordUserFeedback:
    def test_thumbs_up_increments(self, fake_hermes_home):
        record_user_feedback("demo-skill", FEEDBACK_UP)
        rec = get_record("demo-skill")
        assert rec["user_feedback"][FEEDBACK_UP] == 1
        assert rec["user_feedback"][FEEDBACK_DOWN] == 0
        assert rec["user_feedback"]["last_rating"] == FEEDBACK_UP

    def test_thumbs_down_increments(self, fake_hermes_home):
        record_user_feedback("demo-skill", FEEDBACK_DOWN)
        rec = get_record("demo-skill")
        assert rec["user_feedback"][FEEDBACK_DOWN] == 1

    def test_invalid_rating_dropped(self, fake_hermes_home):
        record_user_feedback("demo-skill", "sideways")
        rec = get_record("demo-skill")
        assert rec["user_feedback"][FEEDBACK_UP] == 0
        assert rec["user_feedback"][FEEDBACK_DOWN] == 0

    def test_text_note_added_most_recent_first(self, fake_hermes_home):
        record_user_feedback("demo-skill", FEEDBACK_UP, text="first note")
        record_user_feedback("demo-skill", FEEDBACK_DOWN, text="second note")
        rec = get_record("demo-skill")
        notes = rec["user_feedback"]["notes"]
        assert notes[0] == "second note"
        assert notes[1] == "first note"

    def test_notes_capped_to_max(self, fake_hermes_home):
        max_notes = skill_usage._MAX_FEEDBACK_NOTES
        for i in range(max_notes + 3):
            record_user_feedback("demo-skill", FEEDBACK_UP, text=f"note {i}")
        rec = get_record("demo-skill")
        notes = rec["user_feedback"]["notes"]
        assert len(notes) == max_notes
        # Most-recent-first; first pushed was "note 0"
        assert notes[0] == f"note {max_notes + 2}"
        assert notes[-1] == f"note 3"

    def test_empty_text_not_added(self, fake_hermes_home):
        record_user_feedback("demo-skill", FEEDBACK_UP, text="")
        record_user_feedback("demo-skill", FEEDBACK_UP, text="   ")
        rec = get_record("demo-skill")
        assert rec["user_feedback"]["notes"] == []

    def test_none_text_not_added(self, fake_hermes_home):
        record_user_feedback("demo-skill", FEEDBACK_UP, text=None)
        rec = get_record("demo-skill")
        assert rec["user_feedback"]["notes"] == []


# ---------------------------------------------------------------------------
# compute_skill_score — boundaries
# ---------------------------------------------------------------------------

class TestComputeSkillScore:
    def test_unknown_skill_returns_zero_score(self, fake_hermes_home):
        result = skill_scoring.compute_skill_score("never-seen")
        assert result["score"] == 0.0
        assert result["sample_size"] == 0

    def test_no_outcomes_no_feedback_no_recency_is_zero(self, fake_hermes_home):
        # Record exists but no activity / outcomes / feedback
        record_outcome("demo-skill", OUTCOME_UNKNOWN)
        result = skill_scoring.compute_skill_score("demo-skill")
        # OUTCOME_UNKNOWN doesn't count toward success/failure, so
        # sample_size = 0 and recency = 0 → score 0
        assert result["score"] == 0.0
        assert result["sample_size"] == 0

    def test_all_success_no_feedback_low_confidence(self, fake_hermes_home):
        # 2 successes, no feedback, no last_used_at → low confidence
        record_outcome("demo-skill", OUTCOME_SUCCESS, OUTCOME_SOURCE_AUTO)
        record_outcome("demo-skill", OUTCOME_SUCCESS, OUTCOME_SOURCE_AUTO)
        result = skill_scoring.compute_skill_score("demo-skill")
        assert result["components"]["success_rate"] == 1.0
        assert result["components"]["feedback_score"] == 0.5  # neutral
        # confidence = 2/5 = 0.4
        # blend = 0.6*1.0 + 0.4*0.5 = 0.8
        # recency = 0 (no last_used_at)
        # score = 0.4 * 0.8 + 0.6 * 0 = 0.32
        assert result["score"] == pytest.approx(0.32, abs=0.01)
        assert result["components"]["confidence"] == pytest.approx(0.4)

    def test_all_success_with_thumbs_up_high_confidence(self, fake_hermes_home):
        # 6 successes, 4 thumbs up, recent activity
        for _ in range(6):
            record_outcome("demo-skill", OUTCOME_SUCCESS, OUTCOME_SOURCE_AUTO)
        # bump_use sets last_used_at
        skill_usage.bump_use("demo-skill")
        for _ in range(4):
            record_user_feedback("demo-skill", FEEDBACK_UP)

        result = skill_scoring.compute_skill_score("demo-skill")
        assert result["components"]["success_rate"] == 1.0
        # 4 up / 0 down → (1+1)/2 = 1.0
        assert result["components"]["feedback_score"] == 1.0
        assert result["components"]["confidence"] == 1.0  # 6 ≥ 5
        # blend = 0.6*1.0 + 0.4*1.0 = 1.0 → score 1.0
        assert result["score"] == pytest.approx(1.0, abs=0.001)

    def test_all_failure_with_thumbs_down_low_score(self, fake_hermes_home):
        for _ in range(6):
            record_outcome("demo-skill", OUTCOME_FAILURE, OUTCOME_SOURCE_AUTO)
        skill_usage.bump_use("demo-skill")
        for _ in range(3):
            record_user_feedback("demo-skill", FEEDBACK_DOWN)

        result = skill_scoring.compute_skill_score("demo-skill")
        assert result["components"]["success_rate"] == 0.0
        # 3 down / 0 up → (-1+1)/2 = 0.0
        assert result["components"]["feedback_score"] == 0.0
        assert result["components"]["confidence"] == 1.0
        # blend = 0.6*0 + 0.4*0 = 0 → score 0.0
        assert result["score"] == 0.0

    def test_mixed_thumbs(self, fake_hermes_home):
        for _ in range(6):
            record_outcome("demo-skill", OUTCOME_SUCCESS, OUTCOME_SOURCE_AUTO)
        skill_usage.bump_use("demo-skill")
        # 3 up + 1 down
        for _ in range(3):
            record_user_feedback("demo-skill", FEEDBACK_UP)
        record_user_feedback("demo-skill", FEEDBACK_DOWN)

        result = skill_scoring.compute_skill_score("demo-skill")
        # (3-1)/(3+1) = 0.5 → (0.5+1)/2 = 0.75
        assert result["components"]["feedback_score"] == pytest.approx(0.75, abs=0.01)
        # blend = 0.6*1.0 + 0.4*0.75 = 0.9
        assert result["score"] == pytest.approx(0.9, abs=0.01)

    def test_corrected_outcomes_excluded_from_success_rate(self, fake_hermes_home):
        """Corrected = user had to redo it; should NOT count as success."""
        record_outcome("demo-skill", OUTCOME_SUCCESS, OUTCOME_SOURCE_AUTO)
        record_outcome("demo-skill", OUTCOME_CORRECTED, OUTCOME_SOURCE_USER)
        record_outcome("demo-skill", OUTCOME_SUCCESS, OUTCOME_SOURCE_AUTO)

        result = skill_scoring.compute_skill_score("demo-skill")
        # Denominator = 2 successes only (corrected excluded)
        # success_rate = 2/2 = 1.0
        assert result["components"]["success_rate"] == 1.0
        assert result["sample_size"] == 2

    def test_unknown_outcomes_excluded_from_success_rate(self, fake_hermes_home):
        record_outcome("demo-skill", OUTCOME_SUCCESS, OUTCOME_SOURCE_AUTO)
        record_outcome("demo-skill", OUTCOME_UNKNOWN, OUTCOME_SOURCE_AUTO)
        record_outcome("demo-skill", OUTCOME_SUCCESS, OUTCOME_SOURCE_AUTO)

        result = skill_scoring.compute_skill_score("demo-skill")
        # Denominator = 2 successes (unknown excluded)
        assert result["components"]["success_rate"] == 1.0
        assert result["sample_size"] == 2


# ---------------------------------------------------------------------------
# Recency decay
# ---------------------------------------------------------------------------

class TestRecencyDecay:
    def test_recent_activity_high_recency(self, fake_hermes_home):
        record_outcome("demo-skill", OUTCOME_SUCCESS, OUTCOME_SOURCE_AUTO)
        skill_usage.bump_use("demo-skill")
        result = skill_scoring.compute_skill_score("demo-skill")
        # Just used → recency ~ 1.0
        assert result["components"]["recency_decay"] > 0.99

    def test_old_activity_decays(self, fake_hermes_home, monkeypatch):
        """A skill whose last_used_at was 60 days ago scores ~0.25 on recency
        (half_life = 30)."""
        # Manually write a record with a stale timestamp
        from datetime import datetime, timezone, timedelta
        old_iso = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        path = fake_hermes_home / "skills" / ".usage.json"
        path.write_text(json.dumps({
            "old-skill": {
                "use_count": 1,
                "view_count": 0,
                "patch_count": 0,
                "last_used_at": old_iso,
                "last_viewed_at": None,
                "last_patched_at": None,
                "created_at": old_iso,
                "state": "active",
                "pinned": False,
                "archived_at": None,
                "outcomes": {
                    OUTCOME_SUCCESS: 0, OUTCOME_FAILURE: 0,
                    OUTCOME_CORRECTED: 0, OUTCOME_ABANDONED: 0,
                    OUTCOME_UNKNOWN: 0,
                    "last_outcome": None, "last_outcome_at": None,
                    "last_outcome_source": None,
                },
                "user_feedback": {
                    FEEDBACK_UP: 0, FEEDBACK_DOWN: 0,
                    "last_rating": None, "last_rating_at": None,
                    "notes": [],
                },
            }
        }), encoding="utf-8")
        result = skill_scoring.compute_skill_score("old-skill")
        # 60 days / 30 day half-life = 2 half-lives → 0.5^2 = 0.25
        assert result["components"]["recency_decay"] == pytest.approx(0.25, abs=0.01)


# ---------------------------------------------------------------------------
# score_many
# ---------------------------------------------------------------------------

class TestScoreMany:
    def test_returns_dict_keyed_by_name(self, fake_hermes_home):
        record_outcome("a", OUTCOME_SUCCESS, OUTCOME_SOURCE_AUTO)
        record_outcome("b", OUTCOME_FAILURE, OUTCOME_SOURCE_AUTO)
        results = skill_scoring.score_many(["a", "b", "c"])
        assert set(results.keys()) == {"a", "b", "c"}
        assert results["a"]["sample_size"] == 1
        assert results["b"]["sample_size"] == 1
        assert results["c"]["sample_size"] == 0


# ---------------------------------------------------------------------------
# Score bounds — should always be in [0, 1] regardless of inputs
# ---------------------------------------------------------------------------

class TestScoreBounds:
    @pytest.mark.parametrize("n_success,n_failure,n_up,n_down", [
        (0, 0, 0, 0),
        (10, 0, 10, 0),
        (0, 10, 0, 10),
        (5, 5, 5, 5),
        (100, 100, 100, 100),
        (1, 100, 0, 100),
    ])
    def test_score_in_unit_interval(
        self, fake_hermes_home, n_success, n_failure, n_up, n_down
    ):
        name = f"skill-{n_success}-{n_failure}-{n_up}-{n_down}"
        for _ in range(n_success):
            record_outcome(name, OUTCOME_SUCCESS, OUTCOME_SOURCE_AUTO)
        for _ in range(n_failure):
            record_outcome(name, OUTCOME_FAILURE, OUTCOME_SOURCE_AUTO)
        if n_up or n_down:
            skill_usage.bump_use(name)
        for _ in range(n_up):
            record_user_feedback(name, FEEDBACK_UP)
        for _ in range(n_down):
            record_user_feedback(name, FEEDBACK_DOWN)
        result = skill_scoring.compute_skill_score(name)
        assert 0.0 <= result["score"] <= 1.0, (
            f"score out of bounds: {result['score']} for {name}"
        )
        for comp in result["components"].values():
            assert 0.0 <= comp <= 1.0, f"component {comp} out of bounds"
