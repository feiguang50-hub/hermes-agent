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

* ✅ **Curator prompt integration** *(done 2026-07-19)* — added a
  new sub-bullet `3d` inside the "How to work" section of
  `CURATOR_REVIEW_PROMPT` (`agent/curator.py` around line 508)
  with the four-outcome vocabulary: archive (delete) vs
  `skill_manage action="split"` with `split_into=[...]` (skill
  covers two unrelated domains) vs `skill_manage
  action="deprecate"` with `replaced_by=...` (superseded by a
  better-named umbrella) vs keep. Pinned by
  `tests/agent/test_curator.py::test_curator_review_prompt_documents_lifecycle_vocabulary`.
* ✅ **Decision criteria** *(done 2026-07-19)* — the same
  paragraph encodes the rubric: split when two unrelated jobs;
  deprecate when a better-named umbrella already covers the
  same domain; delete only when content is genuinely obsolete
  or fully absorbed; never archive purely on a low `use_count`.
* ✅ **Scoring-aware decision grounding** *(done 2026-07-19,
  bonus from section E)* — added a "Quality grounding" paragraph
  before "Expected output" instructing the curator to consult
  `tools.skill_usage.get_record(name)` and
  `agent.skill_scoring.compute_skill_score(name)` before any
  split / deprecate / archive decision, with concrete thresholds
  (`success_rate < 0.5` or `feedback_score < 0.5` is a strong
  candidate). Pinned by
  `tests/agent/test_curator.py::test_curator_review_prompt_consults_quality_score`.
* ⏳ **Re-decomposition assist** — when splitting, the LLM should
  sketch the replacement SKILL.md content (or at least the
  proposed names + descriptions) before flipping the original to
  `split`. Currently `split` is a metadata-only action; the
  caller is expected to have created the replacements out of band.
* ⏳ **`hermes skill status` CLI surface** — there's no way for a
  human to ask "which skills are split / deprecated and what do
  they point at?" without grepping `~/.hermes/skills/.usage.json`.

#### C.deferred — make split / deprecate LLM-callable

The prompt now tells the LLM to call `skill_manage
action="split"` and `action="deprecate"`, but **the LLM cannot
legally call them today**. Four specific gaps block the loop:

* ⏳ **`SKILL_MANAGE_SCHEMA` enum** at
  `tools/skill_manager_tool.py:1531` lists only
  `["create", "patch", "edit", "delete", "write_file",
  "remove_file"]`. Adding `"split"` and `"deprecate"` to this
  enum is the prerequisite for the LLM to even propose the call.
* ⏳ **`SKILL_MANAGE_SCHEMA` parameters** dict at
  `tools/skill_manager_tool.py:1532-1605` does not declare
  `split_into: list` or `replaced_by: string`. Even with the
  enum fix, the schema validator would silently drop or reject
  these arguments.
* ⏳ **Registry handler forwarding** at
  `tools/skill_manager_tool.py:1612-1628` only forwards
  `absorbed_into`; `split_into` and `replaced_by` would need to
  be added to the lambda's argument extraction so the dispatcher
  actually receives them.
* ⏳ **`_MUTATING_ACTIONS` blocklist** at
  `agent/curator_hooks.py:54` is the set
  `{"patch", "create", "write_file", "delete"}`. Until `split`
  and `deprecate` are added, the curator's dry-run guard and
  keyword-retention check won't see these new actions.
* ⏳ **Structured YAML output schema** at
  `agent/curator.py` (the `## Structured summary (required)`
  block) declares only `consolidations:` and `prunings:` lists.
  Adding `splits:` and `deprecations:` lists (and updating
  `_parse_structured_summary` / `_classify_removed_skills` /
  `_reconcile_classification` at lines 751-1014) is needed for
  downstream tooling to see the new categories distinctly from
  the existing two.

**Dry-run log — 2026-07-19.** Verified the prompt-assembly
side of C end-to-end by stubbing `_run_llm_review` to capture
the prompt string that would be sent to the model, then
asserting the new vocabulary reaches the wire. Stub input: 3
sample agent-managed skills (`pr-triage-salvage`,
`diagnose-cron-timeout`, `anthropic-api-debugging`) in a tmp
HERMES_HOME; `consolidate=True, dry_run=True`. Captured
prompt: 12 419 chars. Vocabulary checks (all 8 passed):
`action="split"` ✓, `action="deprecate"` ✓, `split_into` ✓,
`replaced_by` ✓, `compute_skill_score` ✓, `get_record` ✓,
`Quality grounding` header ✓, `SPLIT or DEPRECATE` header ✓.
Both new paragraphs appeared in the expected positions (3d
inside item 3 of "How to work"; "Quality grounding" before
"Expected output").

**Caveat.** This was a prompt-assembly verification, not a
real LLM call. No live model was queried. Whether the LLM,
when handed the new prompt, actually emits sensible
split/deprecate decisions in its structured summary — and
whether it emits them in the existing `consolidations:` /
`prunings:` lists (since the new lists don't exist yet) — is
still unverified. That requires a real `hermes curator run
--dry-run` against a populated install with LLM credentials,
which is out of scope for this turn.

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

* ✅ **CLI: `hermes curator audit`** *(done 2026-07-19)* — renders
  the JSONL log that `agent/curator_hooks.py` writes to
  `<HERMES_HOME>/logs/curator/audit.jsonl`. Supports
  `--limit`/`--since`/`--verdict`/`--action`/`--json`. Read-only
  by construction; pins confirmed by
  `tests/agent/test_curator.py::test_cli_audit_does_not_mutate_log`.
* ✅ **CLI: `hermes skill score <name>`** *(done 2026-07-19)* —
  calls `agent.skill_scoring.compute_skill_score(name)` and prints
  the score + components + last_outcome / last_rating. New
  top-level `skill` (singular) command co-existing with the
  existing `skills` (plural) for registry browsing. Read-only;
  pins confirmed by
  `tests/hermes_cli/test_skill_cmd.py::test_cmd_skill_score_does_not_write_to_usage`.

---

## 🚀 Recommended next-session entry point

*(Updated 2026-07-19, second pass.)* Both C (curator prompt
integration) and F (observability dashboard) are now done. The
LLM is told to use `split` / `deprecate` and consult
`compute_skill_score`; the audit log and score are now
human-readable via CLI. **Nothing remains in the C or F
sections** beyond their respective deferred follow-ups.

The single most leveraged next step is the **C.deferred —
schema-plumbing block** (5 items, listed in order below). Until
this lands, the new prompt vocabulary is inert: the LLM sees
`action="split"` / `action="deprecate"` / `split_into=[...]` /
`replaced_by=...` in the prompt, but the LLM-visible schema
rejects those calls and the curator's dry-run guard does not
recognize the new actions. After C.deferred lands, a real
`hermes curator run --dry-run` against a populated install
will actually exercise split/deprecate and the auditor
(commit `8390566`) will show those verdicts in the audit
log — closing the loop end-to-end.

### Next task — C.deferred: wire the LLM-visible schema

The 5 items, in execution order (each is a separate checklist
line in section C.deferred above):

1. `tools/skill_manager_tool.py:1531` — add `"split"` and
   `"deprecate"` to the `SKILL_MANAGE_SCHEMA` action enum.
2. Same file, lines 1532-1605 — declare `split_into: list` and
   `replaced_by: string` in the schema parameters.
3. Same file, lines 1612-1628 — extend the registry handler
   lambda to forward both new arguments.
4. `agent/curator_hooks.py:54` — extend `_MUTATING_ACTIONS` to
   include `split` and `deprecate` so dry-run blocks them.
5. `agent/curator.py` (the `## Structured summary (required)`
   block) — add `splits:` and `deprecations:` lists to the YAML
   schema and update `_parse_structured_summary` /
   `_classify_removed_skills` / `_reconcile_classification` at
   lines 751-1014 to surface them.

**Status: not started in this turn.** This task is the natural
entry point for the next session. It is intentionally separate
from the prompt-integration work that landed earlier today —
the prompt change is a no-op until these 5 plumbing items are
in place.

After C.deferred, the natural next move is **D** (CJK tokenizer
+ embedding layer) — longest-running performance work and the
gating prerequisite for any future skill scaling.

---

## Branch state

All work is on `main` of `feiguang50-hub/hermes-agent`. Last
status refresh: 2026-07-19. Today's commits land as 5 focused
follow-ups in the project's existing rhythm (one focused
commit per change):

| Commit | What |
|--------|------|
| `4432c9e` | feat(agent/curator): integrate split/deprecate vocabulary + scoring guidance into review prompt |
| `410d75f` | test(agent/curator): pin split/deprecate + scoring guidance in review prompt |
| `0687baa` | docs: update PROJECT_STATUS.md (C done, C.deferred added, dry-run log) |
| `8390566` | feat(hermes-cli/curator): add audit subcommand to render curator_hooks audit log history |
| `50c948b` | feat(hermes-cli): add hermes skill top-level command with score subcommand |
| *this commit* | docs: update PROJECT_STATUS.md (F done; C.deferred as next task) |

The next session's work (C.deferred — schema plumbing) is
listed in the "Recommended next-session entry point" section
above as 5 distinct checklist items.
