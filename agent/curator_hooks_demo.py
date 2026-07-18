"""Standalone demo of curator_hooks without running a real LLM.

Drives the pre/post hooks directly to show:
  1. dry-run mode blocks every mutating skill_manage call
  2. real-execution mode approves calls that drop below the retention threshold
  3. real-execution mode allows calls that preserve the target skill's keywords
  4. post_tool_call records execution outcomes alongside the pre-decision

Run with:  python -m agent.curator_hooks_demo
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Make sure we can import agent.* from this directory
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import curator_hooks


# ---------------------------------------------------------------------------
# Setup: create a fake SKILL.md in a temp dir so keyword extraction has data
# ---------------------------------------------------------------------------

def _setup_fake_skill(skills_root: Path) -> None:
    """Write a fake skill directory so extract_skill_keywords has real data."""
    target = skills_root / "pr-triage-salvage"
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(
        "---\n"
        "name: pr-triage-salvage\n"
        "description: Salvage workflow for pull request review failures, "
        "including review-time fixes, regression checks, and CI diagnostics.\n"
        "platforms: [cli, gateway]\n"
        "---\n"
        "\n"
        "# PR Triage Salvage\n"
        "\n"
        "## Configuration\n"
        "\n"
        "Set the review timeout via PR_REVIEW_TIMEOUT env var.\n"
        "\n"
        "## Usage\n"
        "\n"
        "Run salvage when CI fails after a review batch.\n"
        "\n"
        "## Diagnostics\n"
        "\n"
        "Use `gh run watch` to track failing jobs.\n",
        encoding="utf-8",
    )


def _patch_curator_hooks_for_demo(skills_root: Path) -> None:
    """Point the hook at our fake skills root instead of ~/.hermes/skills/."""
    import agent.skill_utils
    # Patch the symbol that the curator hook actually calls.
    # The hook is tolerant of either a single skills dir or a list.
    def _patched_single():
        return skills_root
    def _patched_list():
        return [skills_root]
    agent.skill_utils.get_skills_dir = _patched_single
    agent.skill_utils.get_skills_dirs = _patched_list
    print(f"  (patched get_skills_dir(s) -> {skills_root})")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hr(label: str) -> None:
    print()
    print("=" * 70)
    print(f"  {label}")
    print("=" * 70)


def _show_verdict(label: str, verdict: dict | None) -> None:
    if verdict is None:
        print(f"  {label}: PASS-THROUGH (None)")
        return
    action = verdict.get("action", "?")
    msg = verdict.get("message", "")[:140]
    print(f"  {label}: {action}")
    print(f"     message: {msg}{'...' if len(verdict.get('message','')) > 140 else ''}")
    if "rule_key" in verdict:
        print(f"     rule_key: {verdict['rule_key']}")


def _show_audit_tail(log_path: Path, n: int = 6) -> None:
    print(f"\n  --- last {n} audit records ({log_path.name}) ---")
    if not log_path.exists():
        print("  (no audit log)")
        return
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    for line in lines[-n:]:
        rec = json.loads(line)
        ts = rec.get("ts", "")[:19]
        hook = rec.get("hook", "?")
        verdict = rec.get("verdict", rec.get("status", "?"))
        name = rec.get("name") or "-"
        action = rec.get("action") or "-"
        ratio = rec.get("retention_ratio")
        ratio_s = f"  retention={ratio:.0%}" if isinstance(ratio, (int, float)) else ""
        print(f"    {ts}  {hook:14s}  {verdict:18s}  {action:10s}  {name}{ratio_s}")


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scenario_dry_run_blocks(audit_log: Path) -> None:
    _hr("Scenario 1: dry-run BLOCKS every mutating skill_manage call")

    curator_hooks.enter_curator_context(dry_run=True, audit_log_path=audit_log)

    cases = [
        ("patch attempt", {
            "action": "patch",
            "name": "pr-triage-salvage",
            "file_content": "# This is what the LLM would have written...\nA new PR triage workflow.",
        }),
        ("create attempt", {
            "action": "create",
            "name": "new-umbrella",
            "file_content": "name: new-umbrella\n---\n# New umbrella skill",
        }),
        ("delete attempt", {
            "action": "delete",
            "name": "pr-triage-salvage",
        }),
        ("write_file attempt", {
            "action": "write_file",
            "name": "pr-triage-salvage",
            "file_path": "references/old.md",
            "file_content": "stale content",
        }),
    ]
    for label, args in cases:
        verdict = curator_hooks.curator_pre_tool_call_hook(
            tool_name="skill_manage",
            args=args,
            tool_call_id=f"tc_{label}",
        )
        _show_verdict(label, verdict)
        # Simulate the post-hook firing after the (blocked) tool result
        curator_hooks.curator_post_tool_call_hook(
            tool_name="skill_manage",
            args=args,
            result='{"error": "blocked by curator-guard"}',
            tool_call_id=f"tc_{label}",
            duration_ms=0,
            status="blocked",
            error_type="plugin_block",
        )

    curator_hooks.exit_curator_context()
    _show_audit_tail(audit_log, n=8)


def scenario_real_approval(audit_log: Path) -> None:
    _hr("Scenario 2: real-execution ESCALATES keyword-poor content for approval")

    curator_hooks.enter_curator_context(dry_run=False, audit_log_path=audit_log)

    # Case A: the patch lost the skill's name and most of its keywords
    bad_patch_args = {
        "action": "patch",
        "name": "pr-triage-salvage",
        "file_content": (
            "# Generic code review helper\n\n"
            "This skill helps with code reviews and merge requests.\n"
        ),
    }
    verdict = curator_hooks.curator_pre_tool_call_hook(
        tool_name="skill_manage",
        args=bad_patch_args,
        tool_call_id="tc_bad_patch",
    )
    _show_verdict("bad patch (lost keywords)", verdict)

    # Case B: the patch preserved the name and most keywords
    good_patch_args = {
        "action": "patch",
        "name": "pr-triage-salvage",
        "file_content": (
            "# PR Triage Salvage\n\n"
            "## Configuration\n\n"
            "Set PR_REVIEW_TIMEOUT. Use this for PR review failure salvage, "
            "CI diagnostics, regression checks, and review-time fixes. "
            "Run salvage via `hermes pr-triage-salvage` when CI fails after "
            "a review batch.\n\n"
            "## Usage\n\n"
            "`gh run watch` for diagnostics.\n"
        ),
    }
    verdict = curator_hooks.curator_pre_tool_call_hook(
        tool_name="skill_manage",
        args=good_patch_args,
        tool_call_id="tc_good_patch",
    )
    _show_verdict("good patch (preserved keywords)", verdict)

    # Case C: skill_manage view action — should pass through
    view_args = {"action": "view", "name": "pr-triage-salvage"}
    verdict = curator_hooks.curator_pre_tool_call_hook(
        tool_name="skill_manage",
        args=view_args,
        tool_call_id="tc_view",
    )
    _show_verdict("view (non-mutating)", verdict)

    # Case D: completely unrelated tool — should pass through
    verdict = curator_hooks.curator_pre_tool_call_hook(
        tool_name="terminal",
        args={"command": "ls -la"},
        tool_call_id="tc_terminal",
    )
    _show_verdict("terminal (unrelated tool)", verdict)

    curator_hooks.exit_curator_context()
    _show_audit_tail(audit_log, n=6)


def scenario_chinese_keyword(audit_log: Path, skills_root: Path) -> None:
    _hr("Scenario 3: Chinese skill name + description keyword extraction")

    # Add a Chinese skill
    target = skills_root / "崩铁-音乐-拆解"
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(
        "---\n"
        "name: 崩铁-音乐-拆解\n"
        "description: 崩坏3印象曲多维度音乐拆解框架，包括调性分析、BPM统计、"
        "情感曲线、歌词解析和编曲层次。\n"
        "---\n"
        "\n"
        "# 崩铁音乐拆解\n",
        encoding="utf-8",
    )

    curator_hooks.enter_curator_context(dry_run=False, audit_log_path=audit_log)

    # Patch that drops the keywords entirely
    bad = {
        "action": "patch",
        "name": "崩铁-音乐-拆解",
        "file_content": "# 完全不相关的内容\n这是关于机器学习入门的笔记。\n",
    }
    verdict = curator_hooks.curator_pre_tool_call_hook(
        tool_name="skill_manage",
        args=bad,
        tool_call_id="tc_zh_bad",
    )
    _show_verdict("Chinese bad patch (lost 调性 BPM 情感)", verdict)

    # Patch that keeps them
    good = {
        "action": "patch",
        "name": "崩铁-音乐-拆解",
        "file_content": (
            "# 崩铁音乐拆解 v2\n\n"
            "崩坏3印象曲多维度音乐拆解：调性、BPM、情感曲线、歌词、编曲层次。\n"
        ),
    }
    verdict = curator_hooks.curator_pre_tool_call_hook(
        tool_name="skill_manage",
        args=good,
        tool_call_id="tc_zh_good",
    )
    _show_verdict("Chinese good patch (preserved 调性 BPM 情感)", verdict)

    curator_hooks.exit_curator_context()
    _show_audit_tail(audit_log, n=4)


def scenario_outside_curator() -> None:
    _hr("Scenario 4: hooks are NO-OP outside curator context")
    # Don't enter curator context — hooks should pass through everything
    verdict = curator_hooks.curator_pre_tool_call_hook(
        tool_name="skill_manage",
        args={
            "action": "delete",
            "name": "anything",
        },
        tool_call_id="tc_outside",
    )
    _show_verdict("skill_manage delete (no curator context)", verdict)


def scenario_register_unregister() -> None:
    _hr("Scenario 5: register/unregister lifecycle")
    from agent.curator_hooks import (
        register_curator_hooks, unregister_curator_hooks, are_hooks_registered
    )
    print(f"  before: registered={are_hooks_registered()}")
    a = register_curator_hooks()
    print(f"  register() returned {a}, registered={are_hooks_registered()}")
    b = register_curator_hooks()
    print(f"  register() again returned {b} (idempotent), registered={are_hooks_registered()}")
    c = unregister_curator_hooks()
    print(f"  unregister() returned {c}, registered={are_hooks_registered()}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        skills_root = tmp_path / "skills"
        skills_root.mkdir(parents=True, exist_ok=True)
        audit_log = tmp_path / "curator_audit.jsonl"
        _setup_fake_skill(skills_root)
        _patch_curator_hooks_for_demo(skills_root)

        print(f"  audit log: {audit_log}")
        print(f"  skills root: {skills_root}")

        scenario_dry_run_blocks(audit_log)
        scenario_real_approval(audit_log)
        scenario_chinese_keyword(audit_log, skills_root)
        scenario_outside_curator()
        scenario_register_unregister()

        _hr("Done. Full audit log:")
        if audit_log.exists():
            for line in audit_log.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                name = rec.get("name") or "-"
                print(f"  {rec.get('ts','')[:19]}  "
                      f"{rec.get('hook',''):14s}  "
                      f"{(rec.get('verdict') or rec.get('status','')):18s}  "
                      f"{(rec.get('action') or '-'):10s}  "
                      f"{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
