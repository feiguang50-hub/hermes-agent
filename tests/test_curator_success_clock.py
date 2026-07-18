"""Tests for the curator success-clock split.

Before this fix, ``run_curator_review`` wrote ``last_run_at`` and
``run_count`` *before* the LLM pass even ran. The intent was "a
crash mid-review shouldn't immediately re-trigger", but the side
effect was that a transient provider error — a 5xx, a rate limit,
a network blip — would push the next real attempt out by a full
interval (default 7 days).

The fix splits the clock:

* ``last_success_at`` / ``last_run_at`` / ``run_count`` — bumped
  ONLY after the LLM pass completes without raising. This is what
  ``should_run_now`` keys off.
* ``last_failure_reason`` — set when the LLM pass raises, so a
  human looking at ``hermes curator status`` can see why the last
  attempt didn't complete.

Tests pin both halves: legacy state files (only ``last_run_at``) still
work, and a fresh failure leaves the success clock untouched.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import curator  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_curator_state(tmp_path, monkeypatch):
    """Point curator state at a tmp file so we don't touch the real one."""
    monkeypatch.setattr(curator, "_state_file", lambda: tmp_path / ".curator_state")
    return tmp_path


def _write_state(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _load_state() -> dict:
    return curator.load_state()


# ---------------------------------------------------------------------------
# Default state has the new keys
# ---------------------------------------------------------------------------

class TestDefaultStateHasSuccessClock:
    def test_default_state_includes_last_success_at(self, fake_curator_state):
        state = curator._default_state()
        assert "last_success_at" in state
        assert state["last_success_at"] is None

    def test_default_state_includes_last_failure_reason(self, fake_curator_state):
        state = curator._default_state()
        assert "last_failure_reason" in state
        assert state["last_failure_reason"] is None

    def test_default_state_keeps_legacy_last_run_at(self, fake_curator_state):
        # Backward compat: should_run_now still falls back to last_run_at
        # when last_success_at is missing.
        state = curator._default_state()
        assert "last_run_at" in state


# ---------------------------------------------------------------------------
# should_run_now prefers last_success_at
# ---------------------------------------------------------------------------

class TestShouldRunNowPrefersSuccessClock:
    def test_legacy_state_with_only_last_run_at(self, fake_curator_state):
        """Old state files without last_success_at must still drive the
        scheduler — fall back to last_run_at."""
        state_path = fake_curator_state / ".curator_state"
        # 1 hour ago — well within the default interval (7 days)
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _write_state(state_path, {"last_run_at": recent, "run_count": 5})
        curator.load_state()  # warm up; ignores output
        assert curator.should_run_now() is False

    def test_fresh_success_suppresses_run(self, fake_curator_state):
        state_path = fake_curator_state / ".curator_state"
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _write_state(state_path, {
            "last_run_at": recent,
            "last_success_at": recent,
            "run_count": 5,
        })
        assert curator.should_run_now() is False

    def test_old_success_allows_run(self, fake_curator_state):
        state_path = fake_curator_state / ".curator_state"
        # 30 days ago — well past default 7-day interval
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        _write_state(state_path, {
            "last_run_at": old,
            "last_success_at": old,
            "run_count": 5,
        })
        assert curator.should_run_now() is True

    def test_failed_run_keeps_old_success_clock(self, fake_curator_state):
        """Scenario: last_success_at is 8 days ago (past interval), but
        last_run_at is recent because the curator attempted and failed
        yesterday. should_run_now should still return True — the success
        clock is what gates."""
        state_path = fake_curator_state / ".curator_state"
        old_success = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        recent_attempt = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        _write_state(state_path, {
            "last_run_at": recent_attempt,   # recent attempt that failed
            "last_success_at": old_success,  # last success was 8 days ago
            "last_failure_reason": "provider timeout",
            "run_count": 5,  # unchanged since the failure
        })
        # last_success_at is past interval → allow retry
        assert curator.should_run_now() is True


# ---------------------------------------------------------------------------
# load_state handles missing keys gracefully
# ---------------------------------------------------------------------------

class TestLoadStateBackfills:
    def test_legacy_file_gets_new_keys(self, fake_curator_state):
        state_path = fake_curator_state / ".curator_state"
        _write_state(state_path, {"last_run_at": "2025-01-01T00:00:00Z", "run_count": 3})
        state = curator.load_state()
        # New keys are backfilled with defaults, old values preserved.
        assert state["last_success_at"] is None
        assert state["last_failure_reason"] is None
        assert state["last_run_at"] == "2025-01-01T00:00:00Z"
        assert state["run_count"] == 3


# ---------------------------------------------------------------------------
# Persistence paths: success advances the clock, failure does not
# ---------------------------------------------------------------------------

class TestSuccessAndFailurePersistence:
    """These exercise the success / failure clock branches directly via
    load_state / save_state rather than re-running the full curator
    stack (which would require a real LLM). They pin the *contract*:
    after a clean pass the new fields advance; after a failed pass
    they don't."""

    def test_clean_pass_advances_all_three_clocks(self, fake_curator_state):
        start = datetime.now(timezone.utc).replace(microsecond=0)
        # Simulate the post-success save block from run_curator_review
        state = curator.load_state()
        state["last_success_at"] = start.isoformat()
        state["last_run_at"] = start.isoformat()
        state["run_count"] = int(state.get("run_count", 0)) + 1
        curator.save_state(state)

        loaded = curator.load_state()
        assert loaded["last_success_at"] == start.isoformat()
        assert loaded["last_run_at"] == start.isoformat()
        assert loaded["run_count"] == 1

    def test_failed_pass_preserves_success_clock(self, fake_curator_state):
        # Seed a prior success
        prior = datetime.now(timezone.utc) - timedelta(days=3)
        state_path = fake_curator_state / ".curator_state"
        _write_state(state_path, {
            "last_run_at": prior.isoformat(),
            "last_success_at": prior.isoformat(),
            "run_count": 4,
        })

        # Now simulate a failed pass: load, write only the failure reason
        # and the (refreshed) summary, leave the success clock alone.
        state = curator.load_state()
        state["last_failure_reason"] = "provider timeout"
        state["last_run_summary"] = "auto: llm: error (provider timeout)"
        curator.save_state(state)

        loaded = curator.load_state()
        # success clock UNCHANGED
        assert loaded["last_success_at"] == prior.isoformat()
        assert loaded["last_run_at"] == prior.isoformat()
        assert loaded["run_count"] == 4
        # failure reason recorded
        assert loaded["last_failure_reason"] == "provider timeout"
