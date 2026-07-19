"""Tests for the curator dry-run / retention guard action coverage (R13).

Regression guard for a real gap: ``_MUTATING_ACTIONS`` in
``agent/curator_hooks.py`` gates BOTH the dry-run hard block and the
keyword-retention check. Any mutating ``skill_manage`` action missing from
that set silently bypasses both guards. ``edit`` (full-body replace) and
``remove_file`` were originally omitted, so a curator could route around the
dry-run block with ``skill_manage action="edit"`` / ``action="remove_file"``.

These tests:
  1. pin the set in sync with the schema enum (so a future action addition
     must update both places), and
  2. exercise the bypass scenario end-to-end through the public pre-hook,
     confirming ``edit`` and ``remove_file`` are now blocked in dry-run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import curator_hooks  # noqa: E402
from tools.skill_manager_tool import SKILL_MANAGE_SCHEMA  # noqa: E402


def _schema_actions() -> set:
    return set(
        SKILL_MANAGE_SCHEMA["parameters"]["properties"]["action"]["enum"]
    )


class TestMutatingActionsSet:
    def test_edit_and_remove_file_are_gated(self):
        """The two actions the guard used to miss must now be present."""
        assert "edit" in curator_hooks._MUTATING_ACTIONS
        assert "remove_file" in curator_hooks._MUTATING_ACTIONS

    def test_every_schema_action_is_gated(self):
        """Every action the tool schema accepts is mutating today, so all of
        them must be in _MUTATING_ACTIONS. If a future action is added to the
        schema, this fails until the author decides whether it needs gating —
        that is the point (the guard must not silently miss a mutating verb)."""
        missing = _schema_actions() - set(curator_hooks._MUTATING_ACTIONS)
        assert not missing, (
            f"schema actions not gated by _MUTATING_ACTIONS: {sorted(missing)}"
        )


class TestDryRunBlocksEditAndRemoveFile:
    """End-to-end: the previously-bypassable actions are now hard-blocked in
    dry-run mode, and the block is recorded in the audit log."""

    @pytest.mark.parametrize("action, extra_args", [
        ("edit", {"content": "# rewritten body\n\ntotally different text"}),
        ("remove_file", {"file_path": "references/api-guide.md"}),
    ])
    def test_dry_run_blocks(self, action, extra_args, tmp_path):
        audit_log = tmp_path / "audit.jsonl"
        curator_hooks.enter_curator_context(dry_run=True, audit_log_path=audit_log)
        try:
            args = {"action": action, "name": "some-skill"}
            args.update(extra_args)
            verdict = curator_hooks.curator_pre_tool_call_hook(
                tool_name="skill_manage",
                args=args,
                tool_call_id=f"tc_{action}",
                session_id="sess_r13",
            )
        finally:
            curator_hooks.exit_curator_context()

        assert verdict is not None, f"{action} should have been blocked in dry-run"
        assert verdict["action"] == "block"

        entries = [
            json.loads(line)
            for line in audit_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        blocks = [e for e in entries if e.get("verdict") == "block_dry_run"]
        assert any(e.get("action") == action for e in blocks), (
            f"expected a block_dry_run audit entry for action={action!r}, "
            f"got {[e.get('action') for e in blocks]}"
        )

    def test_non_mutating_action_not_blocked(self, tmp_path):
        """A non-mutating action must still pass straight through even in
        dry-run — the guard should only fire on mutating verbs."""
        audit_log = tmp_path / "audit.jsonl"
        curator_hooks.enter_curator_context(dry_run=True, audit_log_path=audit_log)
        try:
            verdict = curator_hooks.curator_pre_tool_call_hook(
                tool_name="skill_manage",
                args={"action": "view", "name": "some-skill"},
                tool_call_id="tc_view",
                session_id="sess_r13",
            )
        finally:
            curator_hooks.exit_curator_context()
        assert verdict is None
