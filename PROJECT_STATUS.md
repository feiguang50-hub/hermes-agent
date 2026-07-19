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

* ✅ **`SKILL_MANAGE_SCHEMA` enum** at
  `tools/skill_manager_tool.py:1531` — added `"split"` and
  `"deprecate"` to the enum. Pinned by
  `tests/agent/test_curator_classification.py::test_skill_manage_schema_includes_split_and_deprecate`.
* ✅ **`SKILL_MANAGE_SCHEMA` parameters** dict at
  `tools/skill_manager_tool.py:1532-1605` — declared
  `split_into: array` and `replaced_by: string` with the same
  descriptions the prompt uses, so the model knows the exact
  shapes. Pinned by
  `tests/agent/test_curator_classification.py::test_skill_manage_schema_declares_split_into_and_replaced_by`.
* ✅ **Registry handler forwarding** at
  `tools/skill_manager_tool.py:1612-1628` — extended the
  lambda to forward `split_into=args.get("split_into")` and
  `replaced_by=args.get("replaced_by")` to the underlying
  `skill_manage()` call (which already accepts both).
* ✅ **`_MUTATING_ACTIONS` blocklist** at
  `agent/curator_hooks.py:54` — extended to include `split`
  and `deprecate`. The dry-run guard and keyword-retention
  check now see the new actions, so a curator can't route
  around them by flipping state without touching files. Pinned
  by
  `tests/agent/test_curator_classification.py::test_mutating_actions_includes_split_and_deprecate`.
* ✅ **Structured YAML output schema** at
  `agent/curator.py` (the `## Structured summary (required)`
  block) — added `splits:` and `deprecations:` lists and
  updated `_parse_structured_summary` to surface them. Added
  two new helpers — `_extract_lifecycle_declarations` (parses
  `skill_manage(action="split"|"deprecate")` tool calls, the
  authoritative signal) and `_reconcile_lifecycle` (merges
  tool calls with the YAML block, grafting the model's
  `reason` onto each tool-call entry). `_build_rename_summary`
  and `_write_run_report` now emit `splits_this_run` /
  `deprecations_this_run` counts, payload keys, and a
  dedicated markdown section. Pinned by 11 new tests in
  `tests/agent/test_curator_classification.py` (parser,
  extractor, reconciler, summary builder).

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

**C.deferred status (2026-07-19, third pass): done.** All five
plumbing items landed. The LLM can now legally emit
`skill_manage(action="split", split_into=[...])` /
`action="deprecate", replaced_by=...`, the curator's dry-run
guard recognises them, and the YAML block / run.json / REPORT.md
surface them as distinct categories from consolidations /
prunings. A real `hermes curator run --dry-run` against a
populated install will now exercise split/deprecate end-to-end,
and the audit log (commit `8390566`) will show those verdicts
— closing the loop.

**Tool-chain E2E verification (2026-07-19): done.** The plumbing
pathway was exercised end-to-end against real skill data — see
`tests/e2e_test_curator_split_deprecate_dryrun.py` (commit
`d7a081e`). The script writes three real agent-created SKILL.md
files to a tmp `HERMES_HOME`, simulates an LLM emitting one
`split` call and one `deprecate` call (plus the matching YAML
block), and asserts every layer in the chain produced the right
output:

1. `audit.jsonl` shows two `verdict=block_dry_run` entries with the
   curator-guard message and full scrubbed args.
2. `run.json` has `counts.splits_this_run=1`,
   `counts.deprecations_this_run=1`, and full `splits[]` /
   `deprecations[]` arrays with `source="model+audit"` (tool-call
   declaration + YAML reason grafted on).
3. `REPORT.md` renders dedicated `Split into replacement skills`
   and `Deprecated — superseded by umbrella` sections, plus the
   new lines in the LLM pass numbers block.
4. Skill state on disk is unchanged — dry-run correctly blocked
   the writes.

**⚠️ Pending verification (not blocking, lower priority): the LLM
decision itself.** What is verified above is the *tool chain* —
given a scripted response that already uses split / deprecate, the
curator plumbing processes it correctly end-to-end. What is **not**
yet verified is whether the LLM, when handed the new prompt in a
real `hermes curator run --dry-run`, actually emits sensible
`split` / `deprecate` decisions in its structured summary — and
in particular, whether it prefers them over the simpler
`consolidation` / `delete` (absorb into a single umbrella) when
both would apply. The E2E script can't answer that without real
LLM credentials. To verify: run `hermes curator run --dry-run
--consolidate` against a populated install with several skills
that present split-vs-consolidate ambiguity, and check the
resulting `REPORT.md` for non-zero `splits_this_run` /
`deprecations_this_run` counts. If the LLM defaults to
consolidate/delete instead, the prompt vocabulary in
`CURATOR_REVIEW_PROMPT` needs more guidance (probably more
explicit "prefer split when ..." framing). Punted until someone
has API credentials handy and a populated install to point it at.

**Real-LLM verification (2026-07-19, fourth pass): partial pass.**
A real `hermes curator run --dry-run --consolidate` was executed
against 5 real agent-created SKILL.md files using DeepSeek
(`deepseek-chat` via the `deepseek` provider, configured under
`auxiliary.curator` in `~/.hermes/config.yaml`). Provider,
model, and 66.89 s duration are recorded in the resulting
`run.json`. Raw artefacts:

- `~/.hermes/logs/curator/20260719-054629/run.json`
- `~/.hermes/logs/curator/20260719-054629/REPORT.md`
- `~/.hermes/logs/curator/audit.jsonl`

The fixture was designed to give the LLM three categorically
different decisions to make:

| Skill | Designed-for decision |
|---|---|
| `pr-triage-salvage` | **split** — its own SKILL.md says "two flows drifted into one skill" |
| `anthropic-api-debugging` | **deprecate** — fully redundant with the existing `llm-api-debugging` umbrella |
| `openai-api-debugging` | **deprecate** — same |
| `llm-api-debugging` | keep (already the umbrella) |
| `diagnose-cron-timeout` | keep (no siblings) |

Result counts: `splits_this_run=1, deprecations_this_run=0,
consolidated_this_run=2, pruned_this_run=0`. What the LLM
actually decided, and what it tells us:

1. **Split vocabulary IS being used.** `pr-triage-salvage` was
   split into `[pr-triage, pr-salvage]` in the YAML block, with
   the rationale *"covers two unrelated workflows (diagnosis
   vs. cherry-pick salvage)"*. The prompt's split guidance lands
   for genuinely two-topic skills.

2. **Deprecate vocabulary is NOT being used.** Both
   `anthropic-api-debugging` and `openai-api-debugging` got
   `consolidations: [{from → llm-api-debugging}]` in the YAML
   block instead of `deprecations:`. The LLM chose
   `delete absorbed_into=<umbrella>` over `deprecate
   replaced_by=<umbrella>` — even though, semantically, a
   deprecate would be more accurate (the SKILL.md stays on disk
   with a pointer to the umbrella, which is exactly what we want
   when a generic umbrella already supersedes a narrow sibling).
   This is the gap the original C-section prompt-language gap
   test was checking for.

3. **Tool-call discipline held.** The LLM emitted zero
   `skill_manage(action="split"|"deprecate"|"delete")` calls —
   only read-only `skill_view` × 5, `search_files`, `terminal`,
   `read_file` of `.usage.json`. Decisions went into the YAML
   block (`source: "model only"` for the split) rather than the
   tool-call channel. In dry-run this is correct: the prompt
   banner says "produce a report only — no skill_manage
   mutations". In a real `--apply` run we'd expect the model to
   actually invoke the calls, but that wasn't tested here.

**Conclusion.** The plumbing works end-to-end with real LLM
output (`d7a081e`), and the LLM does pick up the new vocabulary
when it fits — at least for the split case. The deprecate case
needs more prompt guidance. Two concrete follow-ups for whoever
takes this next:

- Tighten the `CURATOR_REVIEW_PROMPT` language around
  "deprecate when a better-named umbrella already exists and
  the narrow skill adds no unique content". Currently the prompt
  says *"deprecate when a better-named umbrella already covers
  the same domain"* but the model is still reaching for
  `consolidate` / `delete` first. The likely fix: explicitly
  contrast the two ("deprecate, NOT delete+absorbed_into, when
  the umbrella already exists") and put deprecate ahead of
  consolidate in the decision rubric.
- Re-run this fixture against a model that actually invokes the
  tools (not just the YAML block) to confirm the YAML → tool-call
  pipeline at full fidelity. A `--apply` run would be the
  natural test — but it requires backing up the fixture skills
  first.

**Real-LLM verification (2026-07-19, fifth pass — post-rubric-reorder):
the two designed-deprecate cases now deprecate, but the YAML→tool-call
channel is still unverified.** A second real
`hermes curator run --dry-run --consolidate` was executed against the
**same** five fixture skills as the prior pass (`pr-triage-salvage`,
`anthropic-api-debugging`, `openai-api-debugging`,
`llm-api-debugging`, `diagnose-cron-timeout`) — fixture files
preserved unchanged at `~/.hermes/.fixture-backup-20260719-prepostrubric/`
so the only delta is the prompt text. Same model (`deepseek-chat`
via `deepseek` provider, configured under `auxiliary.curator` in
`~/.hermes/config.yaml`); prompt changes from commits `ac2f8f0`
(deprecate reordering) + `34b293a` (test pin) in place. Raw
artefacts:

- `~/.hermes/logs/curator/20260719-061712/run.json`
- `~/.hermes/logs/curator/20260719-061712/REPORT.md`
- `~/.hermes/logs/curator/audit.jsonl` (curator context session
  `20260719_141717_a43d0c`)

Result counts vs the prior pass:

| Count | PRIOR (old prompt) | NEW (deprecate-first) |
|---|---|---|
| `splits_this_run` | 1 | 1 |
| `deprecations_this_run` | **0** | **2** |
| `consolidated_this_run` | 0 | 0 |
| `pruned_this_run` | 0 | 0 |
| Duration | 66.89 s | 53.13 s |
| Tool calls | 8 | 9 |

Per-skill decision vs the designed-for matrix:

| Skill | Designed-for | PRIOR got | NEW got |
|---|---|---|---|
| `pr-triage-salvage` | split | ✓ split | ✓ split |
| `anthropic-api-debugging` | **deprecate** | consolidate→delete (wrong) | **deprecate replaced_by=llm-api-debugging** ✓ |
| `openai-api-debugging` | **deprecate** | consolidate→delete (wrong) | **deprecate replaced_by=llm-api-debugging** ✓ |
| `llm-api-debugging` | keep | ✓ keep | ✓ keep |
| `diagnose-cron-timeout` | keep | ✓ keep | ✓ keep |

Designed-deprecate count landed: 5/5 — both
`anthropic-api-debugging` and `openai-api-debugging` are now in the
`deprecations:` block with `replaced_by: llm-api-debugging`. The
LLM's `llm_final` text cites the new rubric's vocabulary explicitly:

> **Rationale: Path a (deprecate) applies** — a superset umbrella
> already exists, the narrow siblings add no unique content.

— i.e. the model read the path letterings `a`/`b`/`c`/`d`/`e` and
applied `a` rather than reaching for `consolidations:` first. Both
`deprecations:` reasons also mirror the rubric's
"unique paragraph / example / template" question:

- `anthropic-api-debugging` — *"Strict subset of `llm-api-debugging`
  with zero unique content (the single 'check API key' line is
  trivial boilerplate also applicable to other providers)."*
- `openai-api-debugging` — *"Strict subset of `llm-api-debugging`
  with zero unique content beyond trivial API-key-check preamble."*

**Caveats — what this pass does and does not prove.**

1. **Decisions still go through the YAML block only.** Like the
   prior pass, the model emitted zero
   `skill_manage(action="split"|"deprecate"|"delete")` tool
   calls; both deprecations went into the YAML `deprecations:`
   block (`source: "model only"` in `run.json`, not
   `"tool-call audit"` or `"model+audit"`). In `--dry-run` this is
   correct (the prompt banner says "produce a report only — no
   `skill_manage` mutations"). The **follow-up two paragraphs
   above is still open** — a `--apply` run that exercises the
   YAML → tool-call → audit-log path with the new rubric remains
   undone.
2. **Single run, single model, single temperature.** One
   `deepseek-chat` invocation at default settings. No multi-model
   sweep, no `reasoning_effort: max` variant. A different model —
   or the same model at different sampling settings — could in
   principle revert the rubric effect. This pass confirms that
   this model on this fixture picks up the new vocabulary; it is
   not a guarantee that every model on every fixture will.
3. **Fixture content is unambiguously designed.** The
   provider-specific skills are strictly smaller than the
   umbrella by hand-crafted construction; the rubric's
   "unique paragraph / example / template" call has an obvious
   answer. **A live creator-curator pass against real
   agent-created skills — where the call is a judgement — could
   land differently from this 5/5.** The fixture validates
   that the prompt reaches the LLM and steers the decision
   correctly when the case is unambiguous; it does not validate
   every real-world edge case.
4. **No regression in the non-target skills checked.** `split`
   count stayed at 1, `consolidated_this_run` stayed at 0,
   `pruned_this_run` stayed at 0, the umbrella and the singleton
   are both kept. So reordering deprecate ahead of consolidate
   in the rubric did not regress the other paths on this
   fixture. (Single-fixture limitation applies here too.)

In short: the follow-up at lines 428–436 of this section
(*"put deprecate ahead of consolidate in the decision rubric"* +
*"explicitly contrast the two"*) is satisfied for this fixture on
this model. The companion follow-up at lines 437–441 (a `--apply`
run that exercises the YAML → tool-call channel) is **not**
satisfied by this pass and remains open.

**Real-LLM verification (2026-07-19, sixth pass — boundary fixtures,
`deepseek-chat`): the rubric holds on the no-umbrella case but misroutes
on the 2-bullet-preamble case and under-acts on the real-paragraph case.**

Three new fixtures were designed to test boundary conditions where the
deprecate-vs-merge call isn't obvious — i.e. *closer to real-world*
than the previous hand-crafted fixture set, where the umbrella
strictly contained the siblings. Each fixture was run against a fresh,
isolated `HERMES_HOME` (temp dir under `_boundary_run/`) carrying
only the fixture's three SKILL.md files plus a `config.yaml` with
the `auxiliary.curator: deepseek/deepseek-chat` slot. Same model,
same default temperature. Wall-clock: ~30 s per fixture.

**Fixture A — *trivial preamble only*.**
Sibling `postgres-connection-pooling-heroku` has 2 short bullets of
Heroku-specific content (Heroku's `DATABASE_URL` parsing; managed
pgBouncer forcing transaction mode) plus an explicit self-cue that
everything else is shared with the umbrella
`postgres-connection-pooling`. The sibling skill's own body even says
"The two bullets above are the only Heroku-specific lines; everything
else is shared." A sentinel `diagnose-cron-timeout-v2` is kept
isolated.

- **Designed-for:** `deprecate replaced_by=postgres-connection-pooling`
  (the 2 bullets are not a unique paragraph; they're a labelled
  subsection candidate at most).
- **LLM actually did:** `consolidations: [{from: postgres-connection-
  pooling-heroku, into: postgres-connection-pooling, reason: ...}]`.
- **LLM rationale (verbatim):** *"Heroku-specific content is 2 bullets
  that belong as a labeled subsection under the existing provider-
  agnostic umbrella; the sibling skill's own body says everything
  else is shared."*
- **Read:** **rubric misroutes**. The LLM read "2 bullets" as
  *paragraph* (path c, MERGE) instead of *trivial preamble* (path a,
  DEPRECATE). The DECISION RULE question *"does the narrow skill
  have a unique paragraph, example, or template the umbrella currently
  lacks?"* is too permissive: 2 bullets of unique content is enough
  to flip the LLM into path c. **Suggested rubric tweak (not yet
  applied):** add an explicit "even if 1–2 short bullets are unique,
  prefer path a" line to the DECISION RULE, or rephrase "paragraph"
  to "substantial paragraph (> ~5 lines of body text)".

**Fixture B — *real unique paragraph + template pointer*.**
Sibling `github-actions-concurrency` has a YAML config snippet
(concurrency groups), explicit branching rules (production deploys
must set `cancel-in-progress: false`), and a pointer to a sister
file `templates/concurrency-cancel-warning.yml`. The umbrella
`github-actions-debugging` covers 5 troubleshooting categories but
says nothing about concurrency. Sentinel `hermes-doctor-cache-check`
kept isolated.

- **Designed-for:** `path c — MERGE INTO EXISTING UMBRELLA` (real
  unique content: paragraph + snippet + sibling-file ref).
- **LLM actually did:** *kept all three*. No entries in any YAML
  list. Empty `consolidations:` / `splits:` / `deprecations:` /
  `prunings:`.
- **LLM rationale (verbatim):** *"Both skills relate to GitHub Actions
  but cover **orthogonal concerns**: [concurrency] is a config template;
  [debugging] is a troubleshooting catalog. … Merging them into one
  `github-actions` umbrella would create a skill with no single clear
  trigger — too broad. … This would violate the rule: a human maintainer
  would keep these as two separate reference cards, not one document
  with disjoint sections."*
- **Read:** **rubric under-acts**. The LLM ignored paths c/e entirely
  and reached for "keep" (a legitimate decision per the prompt's
  *"'keep' is a legitimate decision ONLY when…"* clause, but the
  qualifier — "the skill is already a class-level umbrella **and**
  none of the proposed merges would improve discoverability" — does
  not apply here). The LLM also noticed that the concurrency skill's
  referenced template file *"doesn't exist yet"* and used that as
  additional keep-justification. **A second-prompt rubric tweak
  candidate:** tighten the "keep" qualifier to forbid keep-when-
  there's-a-known-overlap-sibling, and surface a "broken cross-
  reference detected" warning so the LLM doesn't use that as an
  excuse to defer.

**Fixture C — *two equal-width siblings, no umbrella exists*.**
Two skills `redis-rate-limit-fixed-window` and
`redis-rate-limit-sliding-window`, each ~6 lines plus a Lua snippet
(one uses `INCR`+`EXPIRE`; the other uses sorted-set
`ZREMRANGEBYSCORE`+`ZCARD`+`ZADD`). Neither is broader than the
other. Sentinel `loose-style-config-error` kept isolated.

- **Designed-for:** `keep both` (no umbrella exists; deprecate doesn't
  apply); possibly create a new umbrella later.
- **LLM actually did:**
  `consolidations: [{from: redis-rate-limit-fixed-window, into: redis-rate-limit},
                    {from: redis-rate-limit-sliding-window, into: redis-rate-limit}]`
  plus `kept: [loose-style-config-error]`.
- **LLM rationale (verbatim):** *"Narrow sibling of sliding-window
  skill; both are two implementations of the same class-level 'Redis
  rate limiting' concern, now under a single labeled umbrella."*
- **Read:** **rubric holds, expressed oddly.** The LLM correctly
  recognised "no umbrella exists" and proposed creating one
  (`redis-rate-limit`) to absorb both siblings — that's path d
  (CREATE A NEW UMBRELLA SKILL.md). But the YAML uses
  `consolidations: [...]` with `into: <new-umbrella-name>` rather
  than introducing a separate `created:` section. The curator's
  reconciliation then has nothing to translate, so neither skill
  appears in `run.json`. End result on disk (under `--apply`) would
  be: a new `redis-rate-limit/SKILL.md` created, both siblings
  archived. That's *behaviourally* path d. The schema convention is
  mismatched, though — a future schema-cleanup item.

**Aggregate (fifth pass + sixth pass).**

Combining all 8 fixtures the curator has been run against under
`deepseek-chat`:

| Fixture | Designed rubric path | LLM did | Verdict |
|---|---|---|---|
| 5-designed: `pr-triage-salvage` | split | split | ✓ exact |
| 5-designed: `anthropic-api-debugging` | deprecate | deprecate | ✓ exact |
| 5-designed: `openai-api-debugging` | deprecate | deprecate | ✓ exact |
| 5-designed: `llm-api-debugging` | keep | (implicit keep) | ✓ exact |
| 5-designed: `diagnose-cron-timeout` | keep | (implicit keep) | ✓ exact |
| Boundary A: `postgres-connection-pooling-heroku` | deprecate | consolidate | ✗ misroutes (path a → c) |
| Boundary B: `github-actions-concurrency` | merge (path c) | keep | ✗ under-acts (path c → keep) |
| Boundary C: `redis-rate-limit-{fixed,sliding}-window` | keep both | consolidate both into new `redis-rate-limit` | ≈ schema-mismatched path d |

Counted strictly, **5/8 exact rubric matches, 1/8 ≈-correct
(semantic path d), 2/8 rubric failures**. Counted leniently
(semantic-correct = pass), **6/8 pass**. The two failures go in
opposite directions:

- Fixture A — the LLM is **too eager to merge** when there's any
  unique content at all.
- Fixture B — the LLM is **too eager to keep** when the two
  skills' content is shaped differently (config template vs
  catalog).

This is a "two tails" picture: the prompt steers the LLM in the
middle but doesn't pin down the boundary calls. Both failures
*would have been caught by the new DECISION RULE if it were
sharper at the 2-bullet end and at the orthogonal-shapes end.*

**`--apply` recommendation (with the failure mode above).** Two
related tests would close out the rubric evaluation:

1. **Constrained `--apply` against the original 5-fixture set** (the
   one where the LLM got all 5 correct). This would exercise the
   YAML → tool-call → audit-log → mutate-disk path end-to-end with
   the *deprecate* vocabulary the rubric is supposed to teach.
   Expected on-disk mutations: small, recoverable from the
   `.archive/` subtree. **Risk: low.**
2. **`--apply` against any boundary-fixture that misroutes.** Less
   informative because the LLM still issues *some* call (e.g.
   Fixture A's `delete absorbed_into=<umbrella>` — the umbrella
   already absorbs the trivial preamble, so the audit log shows one
   `block_dry_run` and one real write). **Risk: medium** —
   `boundary_c_no_umbrella` would *create a new
   `redis-rate-limit/SKILL.md`* and *archive both siblings* on
   disk if the LLM's `into: redis-rate-limit` were a real target.
   That mutation is hard to revert without restoring from the
   pre-apply snapshot.

**Recommended next step: option 1 only.** Defer option 2 until the
two rubric failures (Fixtures A and B) are fixed at the prompt
level — at that point a follow-up dry-run on identical fixtures
should land them on the designed paths before any `--apply` write
is risked.

Branch state additions:

| Commit | What |
|--------|------|
| `ac2f8f0` | feat(agent/curator): put deprecate ahead of consolidate in lifecycle rubric |
| `34b293a` | test(agent/curator): pin deprecate-ahead-of-consolidate ordering |
| `a779001` | docs: record post-rubric real-LLM verification (deprecate-first prompt lands the designed decision on the designed fixture) |
| `cd8e1a5` | docs: record boundary-fixtures real-LLM verification (rubric holds on no-umbrella, misroutes on 2-bullet preamble, under-acts on real-paragraph) |

**Open follow-ups recorded 2026-07-19 (end of sixth pass).**

These are deliberately **not** done in this session. They are recorded
here so the next session has a clean handoff.

1. **`--apply` verification — run against the original 5-fixture
   designed set, *not* against any boundary fixture.** Per the
   recommendation in the "Recommended next step" paragraph above:
   option 1 (5 designed fixtures, LLM already gets 5/5) is the
   right next test because it exercises the YAML → tool-call →
   audit-log → mutate-disk path end-to-end with the *deprecate*
   vocabulary the rubric is supposed to teach, with low on-disk
   risk. The boundary fixtures (3 of them) are deferred for
   `--apply` until their rubric-route failures (see follow-up #2)
   are fixed at the prompt level first.

2. **Two rubric holes exposed by Fixtures A and B — *deferred for a
   dedicated next session, do not act on them now*.** The candidate
   prompt edits are already documented in the per-fixture analysis
   above (Fixture A: tighten "paragraph" to a length threshold;
   Fixture B: tighten the "keep" qualifier and surface broken
   cross-reference as a warning rather than a keep-justification).
   These candidates are kept as-is in this doc; they are **not**
   applied in this session. The user explicitly asked for them to
   be parked here unchanged.

---

---

## Branch state

All work is on `main` of `feiguang50-hub/hermes-agent`. Last
status refresh: 2026-07-19. C.deferred schema plumbing landed as
5 focused follow-ups in the project's existing rhythm (one focused
commit per change), plus an E2E verification script.

| Commit | What |
|--------|------|
| `4432c9e` | feat(agent/curator): integrate split/deprecate vocabulary + scoring guidance into review prompt |
| `410d75f` | test(agent/curator): pin split/deprecate + scoring guidance in review prompt |
| `0687baa` | docs: update PROJECT_STATUS.md (C done, C.deferred added, dry-run log) |
| `8390566` | feat(hermes-cli/curator): add audit subcommand to render curator_hooks audit log history |
| `50c948b` | feat(hermes-cli): add hermes skill top-level command with score subcommand |
| `f7ea70d` | feat(tools/skill-manager): wire split and deprecate through schema (C.deferred #1+#2+#3) |
| `5b78b9b` | fix(agent/curator-hooks): extend mutating actions to split and deprecate (C.deferred #4) |
| `0d81bb7` | feat(agent/curator): surface split and deprecate in structured summary (C.deferred #5) |
| `f71e918` | test: pin C.deferred schema-plumbing (17 focused tests) |
| `66ddcaa` | docs: update PROJECT_STATUS.md — C.deferred done |
| `d7a081e` | test(e2e): curator split/deprecate dry-run end-to-end verification |
| *this commit* | docs: update PROJECT_STATUS.md — E2E verification logged; LLM-inference verification deferred (needs real credentials) |

The tool-chain E2E verification (`d7a081e`) proves the plumbing
pathway — hook interception, YAML parsing, reconciliation, report
generation — works end-to-end against real skill data. A real
DeepSeek run (2026-07-19) confirmed the LLM picks up the
`split` vocabulary end-to-end (`splits_this_run=1`) but defaults
to `consolidate` over `deprecate` for duplicates of an existing
umbrella — see the "Real-LLM verification" note in the C.deferred
section above.
