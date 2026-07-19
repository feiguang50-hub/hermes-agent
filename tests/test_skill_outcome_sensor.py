"""Tests for the skill-outcome sensor wiring (turn-end outcome recording).

The self-improving evaluation loop was missing its INPUT: outcomes/feedback
were never recorded in runtime, so skill_scoring only ever saw recency. This
wires the sensor: a per-turn skill-use set (fed by bump_use), an outcome
inference from end-of-turn signals, and a guarded recorder called at
finalize_turn. These tests cover the three units independently.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def su(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    import tools.skill_usage as skill_usage
    importlib.reload(skill_usage)
    return skill_usage


# ---------------------------------------------------------------------------
# infer_turn_outcome — the outcome policy
# ---------------------------------------------------------------------------

class TestInferTurnOutcome:
    def test_clean_completion_is_success(self, su):
        assert su.infer_turn_outcome(final_response="all done") == su.OUTCOME_SUCCESS

    def test_interrupt_is_abandoned(self, su):
        assert su.infer_turn_outcome(interrupted=True, final_response="x") == su.OUTCOME_ABANDONED

    def test_failed_flag_is_failure(self, su):
        assert su.infer_turn_outcome(failed=True, final_response="x") == su.OUTCOME_FAILURE

    @pytest.mark.parametrize("reason", [
        "error_near_max_iterations(30)",
        "all_retries_exhausted_no_response",
        "interrupted_during_api_call",
        "guardrail_halt",
        "max_iterations_reached(30)",
    ])
    def test_error_exit_reasons_are_failure(self, su, reason):
        assert su.infer_turn_outcome(turn_exit_reason=reason, final_response="x") == su.OUTCOME_FAILURE

    @pytest.mark.parametrize("resp", [None, "", "   ", "(empty)"])
    def test_empty_response_is_failure(self, su, resp):
        assert su.infer_turn_outcome(final_response=resp) == su.OUTCOME_FAILURE

    def test_teardown_error_with_response_is_unknown(self, su):
        assert su.infer_turn_outcome(
            final_response="delivered", cleanup_errors=["boom"]
        ) == su.OUTCOME_UNKNOWN

    def test_normal_finish_reason_is_success(self, su):
        assert su.infer_turn_outcome(
            turn_exit_reason="text_response(finish_reason=stop)", final_response="ok"
        ) == su.OUTCOME_SUCCESS


# ---------------------------------------------------------------------------
# per-turn tracking via bump_use
# ---------------------------------------------------------------------------

class TestTurnSkillTracking:
    def test_no_tracking_before_begin(self, su):
        su.bump_use("alpha")  # must not raise, must not accumulate
        assert su.get_turn_skills_used() == set()

    def test_begin_then_bump_accumulates_deduped(self, su):
        su.begin_turn_skill_tracking()
        su.bump_use("alpha")
        su.bump_use("beta")
        su.bump_use("alpha")
        assert su.get_turn_skills_used() == {"alpha", "beta"}

    def test_begin_resets_previous_turn(self, su):
        su.begin_turn_skill_tracking()
        su.bump_use("alpha")
        su.begin_turn_skill_tracking()  # new turn
        assert su.get_turn_skills_used() == set()


# ---------------------------------------------------------------------------
# record_turn_skill_outcomes — end-to-end recording + curator guard
# ---------------------------------------------------------------------------

class TestRecordTurnSkillOutcomes:
    def test_records_success_for_each_used_skill(self, su):
        su.begin_turn_skill_tracking()
        su.bump_use("alpha")
        su.bump_use("beta")
        n = su.record_turn_skill_outcomes(su.get_turn_skills_used(), su.OUTCOME_SUCCESS)
        assert n == 2
        for name in ("alpha", "beta"):
            rec = su.get_record(name)
            assert rec["outcomes"]["success"] == 1
            assert rec["outcomes"]["last_outcome"] == "success"
            assert rec["outcomes"]["last_outcome_source"] == su.OUTCOME_SOURCE_AUTO

    def test_records_failure(self, su):
        su.begin_turn_skill_tracking()
        su.bump_use("gamma")
        su.record_turn_skill_outcomes(su.get_turn_skills_used(), su.OUTCOME_FAILURE)
        assert su.get_record("gamma")["outcomes"]["failure"] == 1

    def test_no_op_under_background_review(self, su):
        from tools import skill_provenance as sp
        # In the curator's background-review fork, bump_use no-ops (so nothing
        # is tracked) AND record_turn_skill_outcomes must refuse to record —
        # the curator's own pass must never fabricate outcomes.
        token = sp.set_current_write_origin(sp.BACKGROUND_REVIEW)
        try:
            su.begin_turn_skill_tracking()
            su.bump_use("delta")
            assert su.get_turn_skills_used() == set()  # bump_use guarded
            n = su.record_turn_skill_outcomes(["delta"], su.OUTCOME_SUCCESS)
        finally:
            sp.reset_current_write_origin(token)
        assert n == 0
        assert su.get_record("delta")["outcomes"]["success"] == 0
