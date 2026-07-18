"""Tests for the skill-related_skills lint and cleanup scripts.

Both scripts must agree: the cleanup applies what the lint detects.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = ROOT / "scripts"
PY = sys.executable


def _run(script_name: str, *args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(SCRIPTS / script_name), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def skills_repo(tmp_path):
    """Build a tiny skills tree with one valid and one dangling reference."""
    skills_root = tmp_path / "skills"
    skills_root.mkdir()

    # Real skill A
    a = skills_root / "skill-a"
    a.mkdir()
    (a / "SKILL.md").write_text(
        "---\n"
        "name: skill-a\n"
        "description: A is for apple\n"
        "related_skills:\n"
        "  - skill-b\n"
        "  - skill-ghost\n"
        "---\n"
        "# A\n",
        encoding="utf-8",
    )

    # Real skill B (referenced by A)
    b = skills_root / "skill-b"
    b.mkdir()
    (b / "SKILL.md").write_text(
        "---\n"
        "name: skill-b\n"
        "description: B is for banana\n"
        "related_skills:\n"
        "  - skill-a\n"
        "---\n"
        "# B\n",
        encoding="utf-8",
    )

    return skills_root


class TestCheckSkillRelated:
    def test_clean_repo_passes(self, skills_repo, tmp_path):
        # First clean up the dangling in our fixture
        _run("cleanup_dangling_related_skills.py", cwd=skills_repo.parent)
        result = _run("check_skill_related.py", cwd=skills_repo.parent)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK" in result.stdout

    def test_dangling_repo_fails(self, skills_repo, tmp_path):
        # Don't clean — fixture has a dangling ref
        result = _run("check_skill_related.py", cwd=skills_repo.parent)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "FAIL" in result.stderr
        assert "1 dangling" in result.stderr

    def test_verbose_lists_dangling(self, skills_repo, tmp_path):
        result = _run(
            "check_skill_related.py", "--verbose", cwd=skills_repo.parent,
        )
        assert result.returncode == 1
        assert "skill-ghost" in result.stderr
        assert "skill-a" in result.stderr  # the file that referenced the ghost

    def test_missing_root_errors(self, tmp_path):
        bogus = tmp_path / "no-such-dir"
        result = _run("check_skill_related.py", "--skills-root", str(bogus),
                      cwd=tmp_path)
        assert result.returncode == 2


class TestCleanupDanglingRelatedSkills:
    def test_dry_run_does_not_modify_files(self, skills_repo, tmp_path):
        before = (skills_repo / "skill-a" / "SKILL.md").read_text(encoding="utf-8")
        result = _run(
            "cleanup_dangling_related_skills.py", "--dry-run",
            cwd=skills_repo.parent,
        )
        assert result.returncode == 0
        after = (skills_repo / "skill-a" / "SKILL.md").read_text(encoding="utf-8")
        assert before == after, "dry-run must not modify files"

    def test_real_run_drops_dangling(self, skills_repo, tmp_path):
        result = _run(
            "cleanup_dangling_related_skills.py", cwd=skills_repo.parent,
        )
        assert result.returncode == 0
        # After cleanup, A only references B
        import yaml
        text = (skills_repo / "skill-a" / "SKILL.md").read_text(encoding="utf-8")
        # crude check — skill-ghost should be gone
        assert "skill-ghost" not in text
        assert "skill-b" in text

    def test_idempotent(self, skills_repo, tmp_path):
        # Run cleanup twice; second run is a no-op
        _run("cleanup_dangling_related_skills.py", cwd=skills_repo.parent)
        result = _run(
            "cleanup_dangling_related_skills.py", "--dry-run",
            cwd=skills_repo.parent,
        )
        # No dangling lines means clean
        assert "drop dangling" not in result.stdout
