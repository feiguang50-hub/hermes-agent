"""Real-data curator review simulator.

Drives the curator hooks against the **real** SKILL.md corpus shipped
with the hermes-agent repo (``./skills/`` — 73 SKILL.md across 19
top-level skill directories). Simulates the kinds of mutating
``skill_manage`` calls an LLM curator pass is expected to emit when
it runs in production, so we can audit how the hook actually judges
real content — including cases that may be false positives.

The script:
  1. Loads the real skills tree (NOT synthesized fake data).
  2. Runs the pre/post hooks under TWO modes:
       Phase A — ``dry_run=True``  → every mutating call should be BLOCKED
       Phase B — ``dry_run=False`` → keyword retention gate decides allow/approve
  3. Writes a single JSON Lines audit log and prints a summary
     highlighting every ``approve_needed`` verdict so we can spot
     false positives in the keyword extraction.

Run with:  python -m agent.curator_hooks_real
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Monkey-patch skill_utils to point at the real repo skills/ tree
import agent.skill_utils
_repo_skills = ROOT / "skills"
agent.skill_utils.get_all_skills_dirs = lambda: [_repo_skills]

from agent import curator_hooks  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hr(s: str) -> None:
    print()
    print("=" * 78)
    print(f"  {s}")
    print("=" * 78)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _simulate_call(label: str, args: dict, dry_run: bool) -> None:
    """Drive a single pre/post tool-call cycle as if the LLM made it."""
    curator_hooks.curator_pre_tool_call_hook(
        function_name="skill_manage",
        function_args=args,
        tool_call_id=f"tc_{int(time.time()*1000)}",
        session_id="real-data-run",
    )
    curator_hooks.curator_post_tool_call_hook(
        function_name="skill_manage",
        function_args=args,
        result='{"ok": true, "simulated": true}',
        tool_call_id=f"tc_{int(time.time()*1000)}",
        session_id="real-data-run",
        duration_ms=42,
        status="ok" if not dry_run else "blocked",
    )


# ---------------------------------------------------------------------------
# Real-skill operations a curator LLM is likely to attempt
# ---------------------------------------------------------------------------
# These are the kinds of decisions the curator prompt nudges the LLM
# toward: patch a skill with an extra section, merge siblings into an
# umbrella, create a new umbrella, demote a sibling to references/.

def _build_realistic_operations() -> list[dict]:
    """Construct skill_manage invocations grounded in the real skill tree."""
    return [
        # ===== Operations that SHOULD be allowed (retention OK) =====
        {
            "category": "patch (should allow)",
            "args": {
                "action": "patch",
                "name": "github-code-review",
                "file_content": (
                    "# GitHub Code Review\n\n"
                    "## Review workflow\n\n"
                    "Use `gh pr diff` to inspect PR changes, then post inline\n"
                    "review comments via the GitHub REST API. The review covers\n"
                    "code style, test coverage, and CI status checks before\n"
                    "approving the PR. Always check the diff for breaking API\n"
                    "changes and missing test cases.\n"
                ),
            },
        },
        {
            "category": "patch (should allow)",
            "args": {
                "action": "patch",
                "name": "claude-code",
                "file_content": (
                    "# Claude Code\n\n"
                    "## Delegation workflow\n\n"
                    "Delegate coding tasks to the Claude Code CLI. Use this skill\n"
                    "to have Claude Code implement features, fix PR review\n"
                    "feedback, and run its own tests. Configure `claude` in PATH\n"
                    "and pass task context via the prompt.\n"
                ),
            },
        },
        {
            "category": "create umbrella (should allow)",
            "args": {
                "action": "create",
                "name": "github-workflows",
                "file_content": (
                    "# GitHub Workflows\n\n"
                    "Class-level umbrella covering github-code-review,\n"
                    "github-pr-workflow, github-issues, github-repo-management,\n"
                    "github-auth, and codebase-inspection. Use this skill for\n"
                    "end-to-end GitHub workflows: review, PR, issue, repo.\n"
                ),
            },
        },
        {
            "category": "create umbrella (should allow)",
            "args": {
                "action": "create",
                "name": "coding-cli-delegation",
                "file_content": (
                    "# Coding CLI Delegation\n\n"
                    "Delegate coding tasks to external CLI tools: claude-code,\n"
                    "codex, opencode. Configure each CLI with API keys, then\n"
                    "pass the user request as the prompt. Use this umbrella\n"
                    "when the user wants autonomous coding help.\n"
                ),
            },
        },
        {
            "category": "write_file (should allow)",
            "args": {
                "action": "write_file",
                "name": "ascii-art",
                "file_path": "references/fonts.md",
                "file_content": (
                    "# ASCII Art Fonts\n\n"
                    "pyfiglet font reference. Common styles: standard, slant,\n"
                    "small, big, banner, block. Use these in ascii-art for\n"
                    "console output, README headers, and code comments.\n"
                ),
            },
        },
        {
            "category": "delete (should allow — delete doesn't check keywords)",
            "args": {
                "action": "delete",
                "name": "mlops/evaluation/lm-evaluation-harness",
            },
        },

        # ===== Operations that SHOULD escalate to approval (retention fails) =====
        # These simulate the LLM "going off the rails" — wrong target, lost keywords.
        {
            "category": "patch (LOST keywords — should escalate)",
            "args": {
                "action": "patch",
                "name": "github-code-review",
                "file_content": (
                    "# Recipe Collection\n\n"
                    "## Pasta dishes\n\n"
                    "Spaghetti carbonara is a classic Italian pasta with eggs,\n"
                    "cheese, and pancetta. Serve with a side salad and wine.\n"
                ),
            },
        },
        {
            "category": "patch (LOST keywords — should escalate)",
            "args": {
                "action": "patch",
                "name": "claude-code",
                "file_content": (
                    "# Kubernetes Operations\n\n"
                    "## Pod management\n\n"
                    "Use kubectl to manage pods, deployments, and services.\n"
                    "Check cluster health with kubectl get nodes.\n"
                ),
            },
        },
        {
            "category": "patch (PARTIAL retention — borderline)",
            "args": {
                "action": "patch",
                "name": "manim-video",
                "file_content": (
                    "# Math Animations\n\n"
                    "## Setup\n\n"
                    "Install manim via pip. Run with `manim -pqh scene.py`.\n"
                    "Use Manim CE for math and algorithm visualizations.\n"
                ),
            },
        },
        {
            "category": "patch (NEAR-EMPTY — should escalate hard)",
            "args": {
                "action": "patch",
                "name": "huggingface-hub",
                "file_content": "# Misc\n",
            },
        },
        {
            "category": "create umbrella (LOST keywords — should escalate)",
            "args": {
                "action": "create",
                "name": "vague-umbrella",
                "file_content": (
                    "# General Stuff\n\n"
                    "A collection of helpful utilities for various tasks.\n"
                    "Use when you need to do things.\n"
                ),
            },
        },
        {
            "category": "create umbrella (WITH merged_skills mentioning absorbed skill — should allow)",
            "args": {
                "action": "create",
                "name": "vague-umbrella",
                "merged_skills": ["vague-thing", "vague-utility"],
                "file_content": (
                    "# Vague Umbrella\n\n"
                    "Class-level umbrella covering vague-thing and vague-utility.\n"
                    "This skill consolidates the two absorbed siblings under one entry.\n"
                ),
            },
        },
        {
            "category": "create umbrella (WITH merged_skills but content ignores them — should escalate)",
            "args": {
                "action": "create",
                "name": "vague-umbrella",
                "merged_skills": ["vague-thing", "vague-utility"],
                "file_content": (
                    "# Vague Umbrella\n\n"
                    "A collection of helpful utilities for various tasks.\n"
                    "Use when you need to do things.\n"
                ),
            },
        },

        # ===== Operation against a skill with CJK chars in description =====
        {
            "category": "patch (Japanese skill — CJK retention test)",
            "args": {
                "action": "patch",
                "name": "baoyu-infographic",
                "file_content": (
                    "# インフォグラフィック\n\n"
                    "## レイアウト\n\n"
                    "21種類のレイアウトと21種類のスタイル（菫｡諱ｯ蝗ｾ、蜿ｯ隗・喧）\n"
                    "をサポート。Use baoyu for infographic generation.\n"
                ),
            },
        },
    ]


# ---------------------------------------------------------------------------
# Audit log reading + reporting
# ---------------------------------------------------------------------------

def _report(audit_log: Path) -> None:
    if not audit_log.exists():
        print("  (no audit log produced)")
        return
    records = [json.loads(l) for l in audit_log.read_text(encoding="utf-8").splitlines() if l.strip()]

    pre = [r for r in records if r.get("hook") == "pre_tool_call"]
    post = [r for r in records if r.get("hook") == "post_tool_call"]
    verdicts: dict[str, int] = {}
    for r in pre:
        verdicts[r.get("verdict", "?")] = verdicts.get(r.get("verdict", "?"), 0) + 1

    _hr("Audit summary")
    print(f"  Total records: {len(records)}  (pre={len(pre)}, post={len(post)})")
    print("  Pre-hook verdict distribution:")
    for v, n in sorted(verdicts.items(), key=lambda x: -x[1]):
        print(f"    {v:20s}  {n}")

    _hr("All `approve_needed` records (the interesting ones)")
    approve_recs = [r for r in pre if r.get("verdict") == "approve_needed"]
    if not approve_recs:
        print("  (none)")
    for i, r in enumerate(approve_recs, 1):
        print(f"\n  --- approve_needed #{i} ---")
        print(f"    action     : {r.get('action')}")
        print(f"    name       : {r.get('name')}")
        print(f"    retention  : {r.get('retention_ratio', 0):.0%}")
        print(f"    preserved  : {r.get('preserved', [])}")
        print(f"    missing    : {r.get('missing', [])}")
        print(f"    message    : {r.get('message', '')[:200]}")

    _hr("All `allow` records (sanity check — these should make sense)")
    allow_recs = [r for r in pre if r.get("verdict") == "allow"]
    for i, r in enumerate(allow_recs, 1):
        print(f"  allow #{i}: {r.get('action'):10s} {r.get('name'):50s}  "
              f"retention={r.get('retention_ratio', 0):.0%}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    audit_log = ROOT / "curator_hooks_real_audit.jsonl"
    if audit_log.exists():
        audit_log.unlink()

    print(f"  Real skills tree: {_repo_skills}")
    n_skill_dirs = sum(1 for _ in _repo_skills.glob("*/SKILL.md"))
    n_subdir_skill_dirs = sum(1 for _ in _repo_skills.glob("*/*/SKILL.md"))
    print(f"  Top-level SKILL.md: {n_skill_dirs}")
    print(f"  Subdir SKILL.md:    {n_subdir_skill_dirs}")
    print(f"  Audit log:          {audit_log}")

    operations = _build_realistic_operations()
    print(f"  Simulated operations: {len(operations)}")

    # ---- Phase A: dry_run=True — every mutating call should be BLOCKED ----
    _hr("Phase A: dry_run=True (every mutating call BLOCKED)")
    curator_hooks.enter_curator_context(dry_run=True, audit_log_path=audit_log)
    for op in operations:
        _simulate_call(op["category"], op["args"], dry_run=True)
    curator_hooks.exit_curator_context()

    # ---- Phase B: dry_run=False — keyword retention decides allow/approve ----
    _hr("Phase B: dry_run=False (keyword retention decides)")
    curator_hooks.enter_curator_context(dry_run=False, audit_log_path=audit_log)
    for op in operations:
        _simulate_call(op["category"], op["args"], dry_run=False)
    curator_hooks.exit_curator_context()

    # ---- Report ----
    _report(audit_log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
