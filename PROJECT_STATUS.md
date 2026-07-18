# Curator Evaluation Mechanism — Project Status

Last updated: 2026-07-18

This document tracks the work-in-progress on the **self-improving
evaluation / lifecycle redesign** for Hermes Agent's curator. The
goal is a complete feedback loop that:

1. Captures *whether a skill actually helped* (not just whether it
   was loaded).
2. Grades skills on a 0–1 scale derived from that signal.
3. Uses the grade to drive lifecycle decisions (keep, archive,
   **split**, **deprecate**).
4. Closes the loop by surfacing grades back to the curator, which
   then acts on the underperformers.

This is the project the `agent/curator.py`, `agent/curator_hooks.py`,
`agent/memory_manager.py`, `tools/skill_usage.py`, and
`agent/prompt_builder.py` modules exist to support.

---

## ✅ Done

### 1. Three-layer curator-hook validation + four real bugs

Audited the curator LLM pass and the curator_hooks enforcement
layer, found and fixed four real defects in the just-shipped
curator_hooks code, and wrote a verification methodology README.

| Commit  | Purpose |
|--------|---------|
| `82d4db7` | docs(agent/curator-hooks): add README documenting verification methodology and four real bugs |
| `0a486b8` | fix(agent/curator-hooks): align hook kwargs with Hermes invoke_hook contract |
| `2ec45d4` | feat(agent/curator-hooks): accept merged_skills on umbrella create |
| `82f6657` | feat(agent/curator-hooks): dry-run hard block, keyword retention gate, and JSONL audit log |

### 2. Task A — outcome + user-feedback schema + scoring

The "skill was loaded" signal existed but no success / failure /
feedback signal. Added outcome (`success` / `failure` /
`corrected` / `abandoned` / `unknown`) and user-feedback (thumbs up /
down + notes) fields to `tools/skill_usage.py`, plus a
`agent/skill_scoring.py` module that blends them into a 0–1
quality score with a recency-decay floor and a confidence-damped
blend.

| Commit | Purpose |
|--------|---------|
| `1cc09eb` | feat(tools/skill-usage): add outcome + user feedback schema and scoring |

### 3. Bug #2 — terminal bypass

`curator_hooks` only inspected `skill_manage` calls, leaving the
`terminal` tool wide open: a curator could `rm -rf
~/.hermes/skills/foo/SKILL.md` and the dry-run / keyword-retention
guards would not see it. Now any mutating shell command that
targets a skill path is blocked in dry-run mode and escalated to
human approval in real-execution mode.

| Commit | Purpose |
|--------|---------|
| `1a1bfdf` | fix(agent/curator-hooks): gate terminal commands that mutate skill paths |

### 4. Series B — seven cross-module bugs

A focused round of targeted bug fixes that the earlier audit
flagged. Each landed in its own commit with focused tests.

| # | Commit | Bug | Fix |
|---|--------|-----|-----|
| B1 | `084f9c9` | 16 `related_skills` entries pointed at non-existent skills | One-shot cleanup script + CI lint |
| B2 | `a2f20ec` | Consolidation prompt pressured output volume ("fewer than 10 archives → too early") | Replaced with quality framing; left `max_iterations=9999` as documented |
| B3 | `25cbb12` | `description` storage cap was 1024 but routing only showed 60 | Unified on a single 200-char routing cap, single source of truth (`_ROUTING_DESCRIPTION_MAX`) |
| B4 | `7dd2c10` | Curator's own `skill_view` calls reset `last_used_at` / `last_viewed_at`, defeating the inactivity clock | `bump_view` / `bump_use` now short-circuit under the existing `is_background_review()` ContextVar |
| B5 | `90f486b` | `last_run_at` written *before* the LLM pass, so a single transient error suppressed the next attempt for a full interval | Split into `last_success_at` (gated scheduling) and `last_attempt_at` / `last_failure_reason` (operator visibility) |
| B6 | `b64822a` | Lifecycle vocabulary was only `active` / `stale` / `archived` — no way to say "decomposed" or "superseded" | Added `split` / `deprecated` states, `split_into` / `replaced_by` recorders, `skill_manage action="split"` / `"deprecate"`, routing-layer hide |

All B-series work shipped with focused pytest coverage:

* `tests/test_check_skill_related.py`
* `tests/test_description_routing_limit.py`
* `tests/test_curator_telemetry_guard.py`
* `tests/test_curator_success_clock.py`
* `tests/test_lifecycle_states.py`

Plus the carry-over test files for tasks A and Bug #2:

* `tests/test_skill_scoring.py`
* `tests/test_curator_hooks_terminal.py`

Total: **134 new tests, all passing**.

---

## ⏳ Not done (the C / D / E roadmap)

These were on the original 7-bug report but explicitly deferred
until the foundation above was solid. They are next in line.

### C — SPLIT / DEPRECATE state machine *completion*

B6 added the **schema** and the **primitive actions**
(`set_state` / `record_split_into` / `record_replaced_by` /
`skill_manage action="split"` / `"deprecate"`). What's missing for
the lifecycle state machine to be useful:

* **Curator prompt integration** — the `CURATOR_REVIEW_PROMPT` in
  `agent/curator.py` still doesn't tell the LLM when to use
  `split` vs `deprecate` vs `delete` vs `patch` vs `create`.
* **Decision criteria** — when should the curator choose split
  over deprecate? When one skill covers two unrelated domains
  (split). When a better-named umbrella exists (deprecate +
  `replaced_by`). Today the LLM has no rubric.
* **Re-decomposition assist** — when splitting, the LLM should
  sketch the replacement SKILL.md content (or at least the
  proposed names + descriptions) before flipping the original to
  `split`. Currently `split` is a metadata-only action; the
  caller is expected to have created the replacements out of band.
* **`hermes skill status` CLI surface** — there's no way for a
  human to ask "which skills are split / deprecated and what do
  they point at?" without grepping `~/.hermes/skills/.usage.json`.

### D — Skill retrieval layer

Routing today is "dump every skill's name + first 200 chars of
description into the system prompt, let the LLM pick". That works
up to a few dozen skills and breaks down at hundreds:

* **Metadata index** — currently each prompt build re-walks the
  filesystem (or loads a snapshot that doesn't include any
  semantic signal). A persistent index would speed cold builds
  and let us add multi-signal ranking.
* **Optional embedding retrieval** — the
  `learning_graph.py:222-244` "vocabulary overlap" memory-to-skill
  edge is the closest thing to retrieval we have, and it's a regex
  token overlap, not semantic. A real embedding layer (bge-small
  on the frontmatter `description` field, kept locally) would let
  us select top-K before the LLM sees the prompt.
* **CJK keyword optimization** — the agent corpus mixes English
  and Chinese descriptions. The current token-overlap scoring
  treats Chinese as a bag of characters (one char = one token),
  which is rough for `pr-triage-salvage` (English) being scored
  against `崩坏3印象曲多维度音乐拆解` (Chinese). A tokenizer
  that recognizes both is needed before any embedding layer is
  worth building.

### E — curator prompt rebalance (originally listed as separate
from B2)

B2 removed the "fewer than 10 archives → too early" pressure.
What's still loose:

* The prompt still doesn't tell the LLM *how* to evaluate outcome
  data it could read via `compute_skill_score(name)` from
  `agent.skill_scoring`. Right now it has skill counts and dates
  but not scores.
* The "kill 5 tools in a row" guardrail for runaway passes
  exists in the conversation loop but isn't surfaced as a curator
  prompt instruction.

### F — observability dashboard (not in the original report,
discovered during B-series work)

* `audit_log.jsonl` (curator_hooks) is being written but nothing
  reads it. A `hermes curator audit --since=...` CLI would let the
  user see the dry-run block / approve breakdown over time.
* `skill_usage.json` outcome + feedback counts are written but
  nothing renders them. A `hermes skill score <name>` CLI would
  close the human-in-loop half of the feedback loop.

---

## 🚀 Recommended next-session entry point

The most leveraged thing to do next is **C** (curator prompt
integration for SPLIT / DEPRECATE). The infrastructure from B6 is
in place but it's dormant until the curator actually emits those
actions; without the prompt update, B6 is just schema work that
nothing exercises.

Concretely:

1. Open `agent/curator.py` and find `CURATOR_REVIEW_PROMPT` (the
   block that starts around line 400). Add a paragraph describing
   the four-state outcome vocabulary (`active` / `stale` /
   `archived` / `split` / `deprecated`) with concrete decision
   rules: "use `skill_manage action="split"` with `split_into=[…]`
   when the skill is doing two unrelated jobs; use
   `action="deprecate"` with `replaced_by="…"` when a better-named
   umbrella covers the same domain."
2. Add a short scoring-aware paragraph: "before splitting or
   archiving, prefer to consult
   `tools.skill_usage.get_record(name)` and
   `agent.skill_scoring.compute_skill_score(name)` to confirm
   the skill is actually underperforming — not just unused."
3. Verify against `tests/test_lifecycle_states.py` and add a new
   test that mocks the LLM prompt and asserts the new paragraphs
   are present.

After C, the natural next moves are **D** (CJK tokenizer +
embedding layer) because that's the longest-running performance
work and it unblocks future skill scaling.

---

## Branch state

All work is on `main` of `feiguang50-hub/hermes-agent`. No
uncommitted changes. The 13 commits listed above are the
project's full footprint.
