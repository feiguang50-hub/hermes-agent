"""Tests for Bug #2 fix: terminal tool bypassing curator dry-run / retention.

The hook now detects mutating shell commands that target skill paths and
applies the same dry-run block / approval-escalation semantics as
skill_manage would. These tests cover the four layers independently and
end-to-end:

  1. ``_is_mutating_shell_command`` — pattern detection
  2. ``_extract_paths_from_command`` — path extraction from a shell string
  3. ``_path_under_skill_root`` — path-below-skill-dir test
  4. ``_check_terminal_skill_mutation`` — end-to-end verdict in
     dry-run mode, real-execution mode, and pass-through cases.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import curator_hooks  # noqa: E402


# ---------------------------------------------------------------------------
# Layer 1: pattern detection
# ---------------------------------------------------------------------------

class TestIsMutatingShellCommand:
    @pytest.mark.parametrize("cmd,expected", [
        # destructive
        ("rm -rf ~/.hermes/skills/foo", True),
        ("rm ~/.hermes/skills/foo/SKILL.md", True),
        ("rm --help", False),  # --help is not mutation
        ("rm --version", False),
        # move / copy
        ("mv ~/.hermes/skills/foo /tmp/x", True),
        ("cp ~/.hermes/skills/foo /tmp/x", True),
        # in-place edits
        ("sed -i 's/a/b/' ~/.hermes/skills/foo/SKILL.md", True),
        ("perl -i -pe 's/a/b/' ~/.hermes/skills/foo/SKILL.md", True),
        # tee
        ("echo hello | tee ~/.hermes/skills/foo/SKILL.md", True),
        # dd overwrite
        ("dd if=/dev/zero of=~/.hermes/skills/foo/SKILL.md bs=1k count=1", True),
        # redirects
        ("echo hello > ~/.hermes/skills/foo/SKILL.md", True),
        ("echo hello >> ~/.hermes/skills/foo/SKILL.md", True),
        # truncate
        ("truncate -s 0 ~/.hermes/skills/foo/SKILL.md", True),
        # non-mutating (should pass through)
        ("ls -la ~/.hermes/skills", False),
        ("cat ~/.hermes/skills/foo/SKILL.md", False),
        ("grep -r pattern ~/.hermes/skills", False),
        ("mkdir ~/.hermes/skills/new-skill", False),
        ("chmod 644 ~/.hermes/skills/foo/SKILL.md", False),  # conservative: not blocked
        ("touch ~/.hermes/skills/foo/SKILL.md", False),       # conservative
    ])
    def test_pattern(self, cmd, expected):
        assert curator_hooks._is_mutating_shell_command(cmd) is expected, (
            f"expected {expected} for: {cmd!r}"
        )

    def test_empty_command(self):
        assert curator_hooks._is_mutating_shell_command("") is False
        assert curator_hooks._is_mutating_shell_command(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Layer 2: path extraction
# ---------------------------------------------------------------------------

class TestExtractPathsFromCommand:
    def test_extracts_absolute_paths(self):
        cmd = "rm /home/u/.hermes/skills/foo /etc/passwd"
        paths = curator_hooks._extract_paths_from_command(cmd)
        assert "/home/u/.hermes/skills/foo" in paths
        assert "/etc/passwd" in paths

    def test_extracts_tilde_paths(self):
        cmd = "echo hi > ~/.hermes/skills/foo/SKILL.md"
        paths = curator_hooks._extract_paths_from_command(cmd)
        assert "~/.hermes/skills/foo/SKILL.md" in paths

    def test_extracts_relative_paths(self):
        cmd = "cp ./foo ../bar"
        paths = curator_hooks._extract_paths_from_command(cmd)
        assert "./foo" in paths
        assert "../bar" in paths
        # bare names without '/' are NOT paths (too ambiguous)
        cmd2 = "cp foo bar baz"
        paths2 = curator_hooks._extract_paths_from_command(cmd2)
        assert paths2 == []

    def test_skips_flags(self):
        cmd = "rm -rf /tmp/foo"
        paths = curator_hooks._extract_paths_from_command(cmd)
        assert "-rf" not in paths
        assert "/tmp/foo" in paths

    def test_handles_pipes_and_chains(self):
        cmd = "cat /tmp/x | grep y | tee /home/u/.hermes/skills/foo/SKILL.md"
        paths = curator_hooks._extract_paths_from_command(cmd)
        assert "/tmp/x" in paths
        assert "/home/u/.hermes/skills/foo/SKILL.md" in paths

    def test_strips_trailing_punct(self):
        cmd = "rm /tmp/foo; cp /tmp/bar."
        paths = curator_hooks._extract_paths_from_command(cmd)
        assert "/tmp/foo" in paths
        assert "/tmp/bar" in paths

    def test_handles_windows_backslash_paths(self):
        cmd = r"del C:\Users\u\.hermes\skills\foo\SKILL.md"
        paths = curator_hooks._extract_paths_from_command(cmd)
        # Backslash should not split Windows paths
        assert any("SKILL.md" in p and "skills" in p for p in paths), (
            f"Windows-style path not extracted as a single token: {paths}"
        )


# ---------------------------------------------------------------------------
# Layer 3: path-below-skill-dir
# ---------------------------------------------------------------------------

class TestPathUnderSkillRoot:
    def test_path_under_skill_root(self, tmp_path):
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        nested = skill_dir / "foo" / "SKILL.md"
        nested.parent.mkdir()
        nested.touch()
        result = curator_hooks._path_under_skill_root(
            str(nested), [skill_dir]
        )
        assert result is not None
        assert result == nested.resolve()

    def test_path_equal_to_root_is_not_inside(self, tmp_path):
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        result = curator_hooks._path_under_skill_root(
            str(skill_dir), [skill_dir]
        )
        # The root itself is NOT a skill path — we want INSIDE.
        assert result is None

    def test_path_outside_skill_root(self, tmp_path):
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        other = tmp_path / "etc" / "passwd"
        other.parent.mkdir()
        other.touch()
        result = curator_hooks._path_under_skill_root(
            str(other), [skill_dir]
        )
        assert result is None

    def test_tilde_expansion(self, tmp_path, monkeypatch):
        """Path.absolute / expanduser behavior varies by platform —
        test that an explicit absolute path under a skill root is
        detected. (Windows ``expanduser`` ignores HOME; we don't try
        to fight that here — only verify the core path-under-skill
        detection.)
        """
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        nested = skill_dir / "foo"
        nested.mkdir()
        # Test the absolute-path equivalent of ~/skills/foo
        absolute_path = str(skill_dir / "foo")
        result = curator_hooks._path_under_skill_root(
            absolute_path, [skill_dir]
        )
        assert result is not None
        assert result.resolve() == nested.resolve()


# ---------------------------------------------------------------------------
# Layer 4: end-to-end verdict
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_skills_root(tmp_path):
    """Create a fake skills directory with one SKILL.md inside."""
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    skill_dir = skills_root / "demo-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: demo-skill\n"
        "description: Demo skill for terminal-gate tests.\n"
        "---\n"
        "\n"
        "# Demo\n",
        encoding="utf-8",
    )
    return skills_root


@pytest.fixture
def patched_skill_dirs(fake_skills_root):
    """Make get_all_skills_dirs return the fake root."""
    import agent.skill_utils
    original = agent.skill_utils.get_all_skills_dirs
    agent.skill_utils.get_all_skills_dirs = lambda: [fake_skills_root]
    yield
    agent.skill_utils.get_all_skills_dirs = original


class TestCheckTerminalSkillMutation:
    def test_dry_run_blocks_rm_of_skill(self, patched_skill_dirs, fake_skills_root, tmp_path):
        audit_log = tmp_path / "audit.jsonl"
        curator_hooks.enter_curator_context(dry_run=True, audit_log_path=audit_log)
        try:
            target = fake_skills_root / "demo-skill" / "SKILL.md"
            verdict = curator_hooks._check_terminal_skill_mutation(
                args={"command": f"rm -f {target}"},
                state=curator_hooks._state(),
                tool_call_id="tc_test_1",
                session_id="sess_test",
            )
        finally:
            curator_hooks.exit_curator_context()

        assert verdict is not None
        assert verdict["action"] == "block"
        assert "DRY-RUN" in verdict["message"]
        assert "Bug #2" in verdict["message"]
        # Audit log should have the block record
        assert audit_log.exists()
        content = audit_log.read_text(encoding="utf-8")
        assert "block_dry_run_terminal" in content

    def test_dry_run_blocks_redirect_into_skill(self, patched_skill_dirs, fake_skills_root, tmp_path):
        audit_log = tmp_path / "audit.jsonl"
        curator_hooks.enter_curator_context(dry_run=True, audit_log_path=audit_log)
        try:
            target = fake_skills_root / "demo-skill" / "SKILL.md"
            verdict = curator_hooks._check_terminal_skill_mutation(
                args={"command": f"echo 'pwned' > {target}"},
                state=curator_hooks._state(),
                tool_call_id="tc_test_2",
                session_id="sess_test",
            )
        finally:
            curator_hooks.exit_curator_context()

        assert verdict is not None
        assert verdict["action"] == "block"

    def test_real_run_escalates_rm_to_approval(self, patched_skill_dirs, fake_skills_root, tmp_path):
        audit_log = tmp_path / "audit.jsonl"
        curator_hooks.enter_curator_context(dry_run=False, audit_log_path=audit_log)
        try:
            target = fake_skills_root / "demo-skill" / "SKILL.md"
            verdict = curator_hooks._check_terminal_skill_mutation(
                args={"command": f"rm -rf {target}"},
                state=curator_hooks._state(),
                tool_call_id="tc_test_3",
                session_id="sess_test",
            )
        finally:
            curator_hooks.exit_curator_context()

        assert verdict is not None
        assert verdict["action"] == "approve"
        assert "skill" in verdict["message"].lower()
        assert verdict.get("rule_key") == "curator_guard:terminal:skill_mutation"
        content = audit_log.read_text(encoding="utf-8")
        assert "approve_terminal_skill_mutation" in content

    def test_non_mutating_command_passes_through(self, patched_skill_dirs, fake_skills_root, tmp_path):
        audit_log = tmp_path / "audit.jsonl"
        curator_hooks.enter_curator_context(dry_run=True, audit_log_path=audit_log)
        try:
            verdict = curator_hooks._check_terminal_skill_mutation(
                args={"command": f"ls -la {fake_skills_root}/demo-skill"},
                state=curator_hooks._state(),
                tool_call_id="tc_test_4",
                session_id="sess_test",
            )
        finally:
            curator_hooks.exit_curator_context()

        assert verdict is None

    def test_mutating_outside_skill_root_passes_through(self, patched_skill_dirs, fake_skills_root, tmp_path):
        audit_log = tmp_path / "audit.jsonl"
        curator_hooks.enter_curator_context(dry_run=True, audit_log_path=audit_log)
        try:
            verdict = curator_hooks._check_terminal_skill_mutation(
                args={"command": "rm -rf /tmp/some-other-file"},
                state=curator_hooks._state(),
                tool_call_id="tc_test_5",
                session_id="sess_test",
            )
        finally:
            curator_hooks.exit_curator_context()

        assert verdict is None

    def test_no_curator_state_returns_none(self, patched_skill_dirs, fake_skills_root):
        # Without curator context, the outer hook returns None for terminal
        # too — this function is only called when state exists.
        verdict = curator_hooks._check_terminal_skill_mutation(
            args={"command": "rm /tmp/x"},
            state={"dry_run": True},
            tool_call_id="tc_x",
            session_id="s",
        )
        # state exists, but no skill dir — should still allow
        # (fails open if get_all_skills_dirs errors)
        # With patched_skill_dirs, the path /tmp/x is outside, so None
        assert verdict is None

    def test_empty_args_returns_none(self, patched_skill_dirs, fake_skills_root, tmp_path):
        audit_log = tmp_path / "audit.jsonl"
        curator_hooks.enter_curator_context(dry_run=True, audit_log_path=audit_log)
        try:
            assert curator_hooks._check_terminal_skill_mutation(
                args=None, state=curator_hooks._state(),
                tool_call_id="t", session_id="s",
            ) is None
            assert curator_hooks._check_terminal_skill_mutation(
                args={}, state=curator_hooks._state(),
                tool_call_id="t", session_id="s",
            ) is None
            assert curator_hooks._check_terminal_skill_mutation(
                args={"command": ""}, state=curator_hooks._state(),
                tool_call_id="t", session_id="s",
            ) is None
        finally:
            curator_hooks.exit_curator_context()


# ---------------------------------------------------------------------------
# End-to-end through the public hook entry point
# ---------------------------------------------------------------------------

class TestPreHookTerminalIntegration:
    def test_pre_hook_routes_terminal_to_check(self, patched_skill_dirs, fake_skills_root, tmp_path):
        """The public pre_tool_call hook should now route terminal calls
        through _check_terminal_skill_mutation, not bail out as
        'tool != skill_manage'."""
        audit_log = tmp_path / "audit.jsonl"
        curator_hooks.enter_curator_context(dry_run=True, audit_log_path=audit_log)
        try:
            target = fake_skills_root / "demo-skill" / "SKILL.md"
            verdict = curator_hooks.curator_pre_tool_call_hook(
                tool_name="terminal",
                args={"command": f"rm -f {target}"},
                tool_call_id="tc_integration",
                session_id="sess_integration",
            )
        finally:
            curator_hooks.exit_curator_context()

        assert verdict is not None
        assert verdict["action"] == "block"
        # The audit log should NOT have an "observed" entry (we only
        # write observed for tools OTHER than skill_manage/terminal).
        content = audit_log.read_text(encoding="utf-8")
        assert "verdict=observed" not in content
        # It SHOULD have the terminal block
        assert "block_dry_run_terminal" in content

    def test_pre_hook_lets_safe_terminal_pass(self, patched_skill_dirs, fake_skills_root, tmp_path):
        audit_log = tmp_path / "audit.jsonl"
        curator_hooks.enter_curator_context(dry_run=True, audit_log_path=audit_log)
        try:
            verdict = curator_hooks.curator_pre_tool_call_hook(
                tool_name="terminal",
                args={"command": "echo hello"},
                tool_call_id="tc_safe",
                session_id="sess_safe",
            )
        finally:
            curator_hooks.exit_curator_context()

        assert verdict is None
