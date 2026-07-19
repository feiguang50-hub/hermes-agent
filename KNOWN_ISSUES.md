# Known Issues — Curator Lifecycle Review

Last updated: 2026-07-19. Companion to `PROJECT_STATUS.md`.

**Status:** P0 (all 3) + two P1 items (R15, doc contradiction) resolved on
2026-07-19 with focused commits + tests. P1 #5–#7, P2, P3, P4 remain open by
design (P2/P3/P4 explicitly deferred).

This file is the consolidated, prioritized backlog produced by a full read-only
project review (no code changed). It merges: (a) TODOs previously scattered
through `PROJECT_STATUS.md`, (b) new findings from a `curator.py` /
`curator_hooks.py` / `skill_scoring.py` code review, (c) a documentation
contradiction audit, and (d) a test-suite run.

Verification legend: **[verified]** = confirmed by reading the source directly;
**[reported]** = surfaced by the review agent, line refs given, not independently
re-confirmed.

---

## P0 — Real defects that can cause wrong behavior (fix soon)

1. **✅ RESOLVED (commit `69c763d`) — Deterministic prune overwrites
   split/deprecated state.** `[verified]`
   `agent/curator.py:382` — `apply_automatic_transitions` archived any skill
   past the idle threshold whose `state != STATE_ARCHIVED`, including
   `STATE_SPLIT` / `STATE_DEPRECATED`. A deprecated skill idle > archive
   threshold (default 90d) was silently archived, destroying the `replaced_by`
   pointer/stub the design promises. **Fix:** prune now skips `STATE_SPLIT` /
   `STATE_DEPRECATED`; regression test archives a same-age `active` control to
   prove the prune still runs (`tests/agent/test_curator_activity.py`).

2. **✅ RESOLVED (commit `450480a`) — Dry-run / retention guard missed `edit`
   and `remove_file`.** `[verified]`
   `agent/curator_hooks.py` `_MUTATING_ACTIONS` omitted `edit` and
   `remove_file`, both valid mutating schema actions. This set gates BOTH the
   dry-run block AND the keyword-retention check, so a curator `edit`
   (full-body replace) or `remove_file` bypassed both guards. **Fix:** both
   added; a test pins the set in sync with the schema enum and exercises the
   dry-run block for both actions (`tests/test_curator_hooks_mutating_actions.py`).

3. **✅ RESOLVED (commit `e3d8679`) — 3 stale test stubs failed against the
   current `_run_llm_review` signature.** `[verified]`
   `_run_llm_review(prompt, *, dry_run=False)` gained a keyword-only `dry_run`;
   three `tests/agent/test_curator.py` stubs had signature `(prompt)`, so the
   stub raised `TypeError` (swallowed by `curator.py:2084`) and "never ran".
   **Fix:** the 3 invoked stubs now accept `dry_run`. (Note: 2 further
   `test_curator_classification.py` failures on this box are cp932-locale
   artifacts — see P3 item 13; they pass under `PYTHONUTF8=1`.)

---

## P1 — Consistency / robustness / still-unverified

4. **✅ RESOLVED (commit `a6e06e1`) — Staged-replay dropped `split_into` /
   `replaced_by`.** `[verified]`
   `tools/skill_manager_tool.py` `apply_skill_pending` didn't forward those two
   args, so a replayed split/deprecate would fail with "... is required".
   **Fix:** both forwarded; test in `tests/tools/test_write_approval.py`
   captures the forwarded kwargs. (The in-tool gate still excludes split/
   deprecate from staging, so this path is not reachable today — the fix
   removes the latent trap for whoever wires a curator approval path next.)

5. **`split` tool-call path still unverified end-to-end.** (PROJECT_STATUS
   follow-up #3.) The seventh-pass `--apply` deprecated but did not split.
   Do NOT chase by re-running — design a fixture where split is the unambiguous
   decision and confirm the tool-call fires + `state` flips.

6. **Keyword-retention guard blocks legitimate umbrella-enrichment patches.**
   (PROJECT_STATUS follow-up #4; related to R14.) Adding a new section to an
   umbrella dilutes the old-keyword ratio, so legitimate growth reads as
   destructive rewriting and gets blocked. Candidate fixes: additive-aware
   retention (measure preservation, not ratio), a non-interactive approval path
   for the curator's own patches, or prefer `action="edit"` with the full merged
   body.

7. **Broad `except Exception` in the LLM pass masks real errors.** `[verified]`
   `agent/curator.py:2084` downgrades ANY exception in the pass to a debug log +
   "llm: error" summary. This is what made P0-#3 silent. Consider narrowing, or
   at least logging at warning level. *(Still open — not fixed this round.)*

8. **✅ RESOLVED (this commit) — Doc contradiction: `consolidated_this_run`
   count.** `[verified]`
   `PROJECT_STATUS.md` fourth-pass note said `consolidated_this_run=2`; the
   authoritative `20260719-054629/run.json` and the fifth-pass "PRIOR" column
   both show `0`. **Fix:** corrected the fourth-pass line to `0` with a note
   explaining the model's YAML `consolidations:` declaration does not increment
   the *reconciled* consolidation counter.

---

## P2 — Prompt / rubric quality (dedicated next session; currently parked)

9. **Two rubric holes from boundary Fixtures A and B.** (PROJECT_STATUS
   follow-up #2, parked unchanged by user request.) Fixture A: LLM over-merges
   when 1–2 unique bullets exist (tighten "paragraph" to a length threshold).
   Fixture B: LLM over-keeps when two skills are shaped differently (tighten the
   "keep" qualifier; treat a broken cross-reference as a warning, not a
   keep-justification).

10. **Section E loose ends.** The `0.5` success_rate / feedback_score thresholds
    live only in `CURATOR_REVIEW_PROMPT` (~`curator.py:597`) with no
    programmatic backing — `compute_skill_score` never flags "low quality"
    (R8/R19). Related `0.5` constants sit unlinked across three files
    (`NEUTRAL_FEEDBACK_SCORE`, `DEFAULT_RETENTION_THRESHOLD`, the prompt). Also
    the "kill 5 tools in a row" runaway guardrail still isn't surfaced as a
    curator prompt instruction.

---

## P3 — Cleanup / tech debt (low risk; mostly agent-reported)

11. **Dead / vestigial code.** `[reported]` `_resolve_review_model()`
    (`curator.py:2224`, only tests call it); `auto_summary` param of
    `_write_run_report` (`curator.py:1410`, unused in body); `summary_so_far`
    return key (`curator.py:2172`, no readers); `last_failure_reason` (written,
    never surfaced); `score_many()` (`skill_scoring.py:206`, only a test calls).

12. **Stale docstrings.** `[reported]` curator_hooks.py module docstring +
    `curator_hooks.md` still list mutating actions as only patch/create/
    write_file/delete (no split/deprecate); curator.py module docstring omits
    split/deprecate; `skill_scoring.py:153` promises an `"explanation"` key the
    return dict lacks.

13. **Duplicated / inconsistent constants.** `[reported]` `_CONTENT_FIELDS`
    membership differs between `curator_hooks.py:67` and `curator.py:771`;
    arg-truncation is 400 (`curator.py:2429`) vs 200 (`curator_hooks.py:86`);
    multiple `read_text()` calls omit `encoding="utf-8"` — the root cause of 2
    locale-only test failures on a cp932 Windows box, and a latent cross-platform
    risk in any production read of a file containing non-ASCII (em-dashes, smart
    quotes).

---

## P4 — Long-term architecture

14. **Skill retrieval layer (PROJECT_STATUS section D).** Persistent metadata
    index + optional embedding retrieval + **CJK tokenizer** (current
    token-overlap treats Chinese as a bag of characters). Gating prerequisite
    for scaling to hundreds of skills.

---

## Test-suite status (2026-07-19)

- **Curator/skill focused subset:** after this round's fixes, the 3 stale-stub
  failures (P0-#3) are fixed. Remaining on this Windows cp932 box: 2
  locale-only failures in `test_curator_classification.py` (P3 item 13; pass
  under `PYTHONUTF8=1`). New tests added this round:
  `tests/test_curator_hooks_mutating_actions.py` (R13),
  `tests/agent/test_curator_activity.py::test_prune_does_not_archive_split_or_deprecated`
  (R7), and a forwarding test in `tests/tools/test_write_approval.py` (R15).
- **Full project suite:** NOT a clean signal on this machine — a Windows cp932
  locale + missing optional deps (`prompt_toolkit`, `acp`, `pytest_asyncio`,
  `jwt`, `cryptography`, `mcp`, `wcwidth`, ...) produce large numbers of
  environmental errors/failures unrelated to the curator work. A locale-corrected
  (`PYTHONUTF8=1`) full run should be done on a properly-provisioned env (or CI)
  to get a trustworthy aggregate.
