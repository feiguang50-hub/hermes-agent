"""End-to-end verification of the C.deferred schema-plumbing chain.

Goal: prove the full path from a curator LLM decision → split/deprecate
tool call → curator_hooks intercept → audit-log entry → YAML block
parse → run.json / REPORT.md surface actually works against real skill
data, not just unit-test stubs.

Pipeline simulated (no live LLM credentials required):

  run_curator_review(dry_run=True, consolidate=True, synchronous=True)
    └─ _run_llm_review (stubbed)
         ├─ invokes curator_pre_tool_call_hook for each tool call
         │   → writes JSONL audit entries (verdict=blocked in dry-run)
         └─ returns tool_calls + final text with YAML block
              → _write_run_report
                   → _parse_structured_summary splits/deprecations
                   → _extract_lifecycle_declarations
                   → _reconcile_lifecycle
                   → run.json + REPORT.md

The hook chain is exercised by calling curator_pre_tool_call_hook
inside the stubbed _run_llm_review — exactly what a real AIAgent fork
would do at tool-call time.

Assertions (see end of file):
  1. Audit log has blocked verdicts for the split + deprecate calls.
  2. run.json has splits/deprecations populated from both tool calls.
  3. run.json counts include splits_this_run=1, deprecations_this_run=1.
  4. REPORT.md has dedicated "Split into replacement skills" and
     "Deprecated — superseded by umbrella" sections.
  5. The skill state on disk has NOT flipped (dry-run blocks writes).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Ensure the repo root is on sys.path first so its `tools/` package
# wins over the test-tree's `tests/tools/` shadow.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) in sys.path:
    sys.path.remove(str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Setup: isolated HERMES_HOME
# ---------------------------------------------------------------------------

HERMES_HOME = Path(tempfile.mkdtemp(prefix="e2e_curator_"))
os.environ["HERMES_HOME"] = str(HERMES_HOME)
# Pin Path.home so hermes_constants picks up the same root
import pathlib
_real_home = pathlib.Path.home
pathlib.Path.home = lambda: HERMES_HOME

# Pre-create dirs curator / skill_usage expect.
(HERMES_HOME / "skills").mkdir(parents=True)
(HERMES_HOME / "logs").mkdir(parents=True)
(HERMES_HOME / "logs" / "curator").mkdir(parents=True)

print(f"[E2E] HERMES_HOME = {HERMES_HOME}")


# ---------------------------------------------------------------------------
# Create real skill files + mark them agent-created
# ---------------------------------------------------------------------------

SKILL_FIXTURES = [
    # Skill that will be split into two narrower skills
    ("pr-triage-salvage",
     "PR triage and salvage procedures.\nCovers both initial triage and the follow-up salvage flow.\n"),
    # Skill that will be deprecated in favour of a better-named umbrella
    ("anthropic-api-debugging",
     "Anthropic API debugging cheatsheet.\n"),
    # Standalone skills left untouched (control)
    ("diagnose-cron-timeout",
     "Cron timeout diagnostic flow.\n"),
]

SKILL_MD_TEMPLATE = """---
name: {name}
description: |
  Real fixture skill for the C.deferred E2E dry-run test.
---

# {name}

{body}
"""


def _write_skill(name: str, body: str) -> None:
    skill_dir = HERMES_HOME / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        SKILL_MD_TEMPLATE.format(name=name, body=body),
        encoding="utf-8",
    )


for name, body in SKILL_FIXTURES:
    _write_skill(name, body)
    print(f"[E2E] wrote skill: {name}")

# Mark all three as agent-created so the curator treats them as candidates.
from tools import skill_usage as _skill_usage
for name, _ in SKILL_FIXTURES:
    _skill_usage.mark_agent_created(name)
    # Pin an active state and a recent use_count so they show up in the report.
    _skill_usage.set_state(name, _skill_usage.STATE_ACTIVE)
    _skill_usage.bump_use(name)

before_report = _skill_usage.agent_created_report()
before_names = {r["name"] for r in before_report}
print(f"[E2E] before_names = {sorted(before_names)}")
assert before_names == {"pr-triage-salvage", "anthropic-api-debugging",
                        "diagnose-cron-timeout"}, \
    f"Expected 3 agent-created skills, got {before_names}"


# ---------------------------------------------------------------------------
# Stub _run_llm_review to:
#   1. Fire curator_pre_tool_call_hook for each split/deprecate call (so
#      the audit log captures the verdicts exactly as a real AIAgent would).
#   2. Return the YAML block + tool_calls dict that _write_run_report consumes.
# ---------------------------------------------------------------------------

# Reload curator so it picks up the new env / HERMES_HOME
from agent import curator as _curator
import hermes_constants
import importlib
importlib.reload(hermes_constants)
importlib.reload(_curator)

# Reload curator_hooks too — same reason.
from agent import curator_hooks as _curator_hooks
importlib.reload(_curator_hooks)


# The LLM's scripted response: two tool calls + a final text with YAML.
LLM_TOOL_CALLS = [
    {
        "name": "skill_manage",
        "arguments": json.dumps({
            "action": "split",
            "name": "pr-triage-salvage",
            "split_into": ["pr-triage", "salvage-procedures"],
        }),
    },
    {
        "name": "skill_manage",
        "arguments": json.dumps({
            "action": "deprecate",
            "name": "anthropic-api-debugging",
            "replaced_by": "llm-api-debugging",
        }),
    },
]

LLM_FINAL = (
    "Reviewed the candidate list. pr-triage-salvage covers two unrelated "
    "domains (initial triage vs. follow-up salvage) so I'm splitting it. "
    "anthropic-api-debugging is superseded by the better-named llm-api-debugging "
    "umbrella. diagnose-cron-timeout left alone.\n\n"
    "## Structured summary (required)\n"
    "```yaml\n"
    "consolidations: []\n"
    "prunings: []\n"
    "splits:\n"
    "  - name: pr-triage-salvage\n"
    "    into: [pr-triage, salvage-procedures]\n"
    "    reason: covers two unrelated workflows that drifted into one skill\n"
    "deprecations:\n"
    "  - name: anthropic-api-debugging\n"
    "    replaced_by: llm-api-debugging\n"
    "    reason: better-named umbrella covers the same domain\n"
    "```\n"
)


def _stub_llm_review(prompt: str, *, dry_run: bool = False) -> dict:
    """Fake LLM pass that exercises every C.deferred layer.

    The real `_run_llm_review` spawns an AIAgent fork that calls
    tools through the registry. We can't run a live LLM here, so we
    inline the parts that matter:

      1. Enter curator hook context (the real path does this BEFORE
         spawning the agent — see lines 2258-2264 of curator.py).
         Without this, curator_pre_tool_call_hook returns None
         immediately (no curator context → no audit log write).
      2. For each tool call, invoke curator_pre_tool_call_hook
         exactly as the agent's tool dispatcher would. This is what
         writes the JSONL audit entry and (in dry-run) returns the
         block verdict.
      3. Exit curator context in `finally` so the audit handle is closed.
      4. Return tool_calls + final text in the shape _write_run_report
         expects.
    """
    audit_log = HERMES_HOME / "logs" / "curator" / "audit.jsonl"
    _curator_hooks.enter_curator_context(dry_run=dry_run, audit_log_path=audit_log)
    try:
        # Fire the hooks — same code path the AIAgent fork would trigger
        # when dispatching each tool call through registry.
        for i, tc in enumerate(LLM_TOOL_CALLS):
            args = json.loads(tc["arguments"])
            verdict = _curator_hooks.curator_pre_tool_call_hook(
                tool_name="skill_manage",
                args=args,
                tool_call_id=f"call_{i}",
                session_id="e2e-test-session",
                task_id="e2e",
                turn_id="e2e-turn-0",
            )
            # In dry-run, hook returns {"action": "block", ...} — the real
            # agent would surface this as a tool error to the LLM. We log it
            # so we can verify the chain end-to-end.
            print(f"[E2E] hook verdict for {args.get('action')!r} "
                  f"name={args.get('name')!r}: {verdict}")
    finally:
        _curator_hooks.exit_curator_context()
    return {
        "final": LLM_FINAL,
        "summary": "split 1, deprecate 1",
        "model": "e2e-stub",
        "provider": "e2e-stub",
        "tool_calls": LLM_TOOL_CALLS,
        "error": None,
    }


# Patch the live module's _run_llm_review.
_curator._run_llm_review = _stub_llm_review

# Force the curator to use consolidate=True regardless of config so the
# LLM pass runs even on an install with curator.consolidate off.
def _fake_get_consolidate() -> bool:
    return True
_curator.get_consolidate = _fake_get_consolidate

# Avoid mutating state on disk during the "auto-transitions" pre-pass
# (dry_run=True already skips it, but be defensive).
print("[E2E] --- invoking run_curator_review ---")
result = _curator.run_curator_review(
    synchronous=True,
    consolidate=True,
    dry_run=True,
)
print(f"[E2E] result.error = {result.get('error')!r}")
print(f"[E2E] result.summary = {result.get('summary', '')[:200]!r}")


# ---------------------------------------------------------------------------
# Assertions — full chain
# ---------------------------------------------------------------------------

print()
print("=" * 72)
print("CHAIN VERIFICATION")
print("=" * 72)

failures: list[str] = []

# --- (1) Audit log -------------------------------------------------------
audit_log = HERMES_HOME / "logs" / "curator" / "audit.jsonl"
assert audit_log.exists(), f"audit log not written: {audit_log}"
audit_entries = [json.loads(line) for line in audit_log.read_text(
    encoding="utf-8").splitlines() if line.strip()]
print(f"[1] audit.jsonl: {len(audit_entries)} entries")

# In dry-run mode, every skill_manage mutating call should be 'block_dry_run'.
blocked = [e for e in audit_entries
           if e.get("verdict") == "block_dry_run"
           and e.get("tool") == "skill_manage"]
print(f"    blocked skill_manage entries: {len(blocked)}")
for e in blocked:
    print(f"      action={e.get('action')!r} name={e.get('name')!r} "
          f"verdict={e.get('verdict')!r} "
          f"tool_call_id={e.get('tool_call_id')!r}")

blocked_actions = {(e.get("action"), e.get("name")) for e in blocked}
if ("split", "pr-triage-salvage") not in blocked_actions:
    failures.append(
        f"audit.jsonl: expected block_dry_run entry for split/pr-triage-salvage, "
        f"got {blocked_actions}")
if ("deprecate", "anthropic-api-debugging") not in blocked_actions:
    failures.append(
        f"audit.jsonl: expected block_dry_run entry for "
        f"deprecate/anthropic-api-debugging, got {blocked_actions}")

# Every block entry must carry the full scrubbed args + a message naming
# the curator-guard, so a future operator can reproduce the decision.
for e in blocked:
    if e.get("message", "").startswith("[curator-guard]") is False:
        failures.append(
            f"audit.jsonl entry missing curator-guard message: {e!r}")
    if "args" not in e or not isinstance(e["args"], dict):
        failures.append(
            f"audit.jsonl entry missing args dict: {e!r}")

# --- (2) run.json --------------------------------------------------------
# Find the latest run dir under logs/curator/.
run_dirs = sorted((HERMES_HOME / "logs" / "curator").glob("*/"))
assert run_dirs, "no run directory written"
run_dir = run_dirs[-1]
run_json = run_dir / "run.json"
assert run_json.exists(), f"run.json not written: {run_json}"
payload = json.loads(run_json.read_text(encoding="utf-8"))
print(f"[2] {run_json.relative_to(HERMES_HOME)}")
print(f"    counts.splits_this_run = {payload['counts'].get('splits_this_run')}")
print(f"    counts.deprecations_this_run = "
      f"{payload['counts'].get('deprecations_this_run')}")
print(f"    splits entries = {len(payload.get('splits', []))}")
print(f"    deprecations entries = {len(payload.get('deprecations', []))}")

if payload["counts"].get("splits_this_run") != 1:
    failures.append(
        f"counts.splits_this_run: expected 1, got "
        f"{payload['counts'].get('splits_this_run')}")
if payload["counts"].get("deprecations_this_run") != 1:
    failures.append(
        f"counts.deprecations_this_run: expected 1, got "
        f"{payload['counts'].get('deprecations_this_run')}")

# Splits payload entries: 1 entry from tool-call audit, reason from YAML.
splits = payload.get("splits", [])
if len(splits) != 1:
    failures.append(f"payload['splits']: expected 1 entry, got {len(splits)}")
elif splits[0].get("name") != "pr-triage-salvage":
    failures.append(
        f"payload['splits'][0].name: expected 'pr-triage-salvage', got "
        f"{splits[0].get('name')!r}")
elif splits[0].get("into") != ["pr-triage", "salvage-procedures"]:
    failures.append(
        f"payload['splits'][0].into: expected ['pr-triage', 'salvage-procedures'],"
        f" got {splits[0].get('into')!r}")
elif splits[0].get("reason") != "covers two unrelated workflows that drifted into one skill":
    failures.append(
        f"payload['splits'][0].reason: expected YAML reason, got "
        f"{splits[0].get('reason')!r}")
elif splits[0].get("source") != "model+audit":
    failures.append(
        f"payload['splits'][0].source: expected 'model+audit' (tool call + YAML),"
        f" got {splits[0].get('source')!r}")

# Deprecations payload entries.
deps = payload.get("deprecations", [])
if len(deps) != 1:
    failures.append(f"payload['deprecations']: expected 1 entry, got {len(deps)}")
elif deps[0].get("name") != "anthropic-api-debugging":
    failures.append(
        f"payload['deprecations'][0].name: expected 'anthropic-api-debugging',"
        f" got {deps[0].get('name')!r}")
elif deps[0].get("replaced_by") != "llm-api-debugging":
    failures.append(
        f"payload['deprecations'][0].replaced_by: expected 'llm-api-debugging',"
        f" got {deps[0].get('replaced_by')!r}")
elif deps[0].get("reason") != "better-named umbrella covers the same domain":
    failures.append(
        f"payload['deprecations'][0].reason: expected YAML reason, got "
        f"{deps[0].get('reason')!r}")
elif deps[0].get("source") != "model+audit":
    failures.append(
        f"payload['deprecations'][0].source: expected 'model+audit' (tool call + YAML),"
        f" got {deps[0].get('source')!r}")

# --- (3) REPORT.md -------------------------------------------------------
report_md_path = run_dir / "REPORT.md"
assert report_md_path.exists(), f"REPORT.md not written: {report_md_path}"
md = report_md_path.read_text(encoding="utf-8")
print(f"[3] {report_md_path.relative_to(HERMES_HOME)}")
for header in [
    "### Split into replacement skills (1)",
    "### Deprecated — superseded by umbrella (1)",
    "`pr-triage-salvage` → split into [`pr-triage`, `salvage-procedures`]",
    "— covers two unrelated workflows that drifted into one skill",
    "`anthropic-api-debugging` → superseded by `llm-api-debugging`",
    "— better-named umbrella covers the same domain",
]:
    if header not in md:
        failures.append(f"REPORT.md missing: {header!r}")

# LLM pass numbers section should mention splits/deprecations counts.
if "split into replacements: **1**" not in md:
    failures.append("REPORT.md LLM pass section missing 'split into replacements: **1**'")
if "deprecated (superseded by umbrella): **1**" not in md:
    failures.append(
        "REPORT.md LLM pass section missing 'deprecated (superseded by umbrella): **1**'")

# --- (4) Skill state on disk was NOT mutated (dry-run blocks writes) ---
print(f"[4] skill state on disk")
after_report = _skill_usage.agent_created_report()
state_after = {r["name"]: r.get("state") for r in after_report}
print(f"    states: {state_after}")

# In dry-run, the split/deprecate calls were blocked at the hook layer, so
# the underlying state records must still be STATE_ACTIVE — the curator
# did not write STATE_SPLIT / STATE_DEPRECATED.
expected_states = {
    "pr-triage-salvage": "active",
    "anthropic-api-debugging": "active",
    "diagnose-cron-timeout": "active",
}
for name, expected in expected_states.items():
    actual = state_after.get(name)
    if actual != expected:
        failures.append(
            f"skill state: {name} expected {expected!r} (dry-run blocks writes), "
            f"got {actual!r}")

# Confirm: no record_split_into / record_replaced_by side effects happened.
prt = _skill_usage.load_usage().get("pr-triage-salvage", {})
anth = _skill_usage.load_usage().get("anthropic-api-debugging", {})
if "split_into" in prt:
    failures.append(
        f"pr-triage-salvage.split_into unexpectedly written during dry-run: "
        f"{prt.get('split_into')!r}")
if "replaced_by" in anth:
    failures.append(
        f"anthropic-api-debugging.replaced_by unexpectedly written during dry-run: "
        f"{anth.get('replaced_by')!r}")

# --- Verdict -------------------------------------------------------------
print()
print("=" * 72)
if failures:
    print(f"FAIL — {len(failures)} assertion(s) broken:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("PASS — full C.deferred chain verified end-to-end:")
    print("  1. curator_hooks intercepted split + deprecate calls (audit log)")
    print("  2. _parse_structured_summary extracted YAML blocks")
    print("  3. _extract_lifecycle_declarations + _reconcile_lifecycle merged")
    print("     tool-call audit with YAML reason (source='model+audit')")
    print("  4. _write_run_report surfaced splits/deprecations in run.json")
    print("  5. _render_report_markdown emitted dedicated sections")
    print("  6. dry-run correctly blocked writes (state unchanged on disk)")
    print()
    print(f"Artifacts (kept under {HERMES_HOME}):")
    print(f"  {audit_log}")
    print(f"  {run_json}")
    print(f"  {report_md_path}")
    print()
    print("===== AUDIT LOG =====")
    print(audit_log.read_text(encoding="utf-8"))
    print("===== RUN.JSON (counts + splits + deprecations) =====")
    payload_text = json.dumps({
        "counts": payload["counts"],
        "tool_call_counts": payload.get("tool_call_counts"),
        "splits": payload.get("splits"),
        "deprecations": payload.get("deprecations"),
        "split_names": payload.get("split_names"),
        "deprecated_names": payload.get("deprecated_names"),
    }, indent=2, ensure_ascii=False)
    print(payload_text)
    print("===== REPORT.md (relevant sections) =====")
    md = report_md_path.read_text(encoding="utf-8")
    # Print only the sections the E2E cares about.
    keep = False
    for line in md.splitlines():
        if line.startswith("### ") or line.startswith("## "):
            keep = any(h in line for h in [
                "Split into replacement skills",
                "Deprecated — superseded by umbrella",
                "LLM consolidation pass",
            ])
        if keep:
            print(line)
    sys.exit(0)