# agent/curator_hooks.py — Design, Bugs Found, and Verification Methodology

This document is the story of how a small set of pre/post_tool_call
hooks for the curator LLM pass were designed, validated, and debugged
across three rounds of testing. It exists because the methodology
matters more than the code — every bug surfaced required a specific
kind of test to detect, and we want future contributors to know which
test catches what.

## 1. What this module does

`agent/curator_hooks.py` adds two `pre_tool_call` / `post_tool_call`
hooks that fire only while the curator LLM pass is running:

- **pre hook (`curator_pre_tool_call_hook`)** — gates `skill_manage`
  calls based on the curator's current state:
  - **Dry-run hard block.** When `curator.dry_run=True`, every
    mutating `skill_manage` call (patch / create / write_file / delete)
    returns `{"action": "block", "message": ...}` BEFORE the tool
    executes. This is independent of the LLM following the
    `CURATOR_DRY_RUN_BANNER` text in the prompt — if the LLM still
    calls a mutating tool, the hook stops it.
  - **Keyword retention check.** When dry-run is off, the new
    `file_content` / `content` / `new_string` is checked against the
    target skill's identity keywords (name + description keywords +
    section headings + any `merged_skills` / `absorbed_from` /
    `source_skills` / `cluster_members` / `absorbed_skills` argument
    passed in for umbrella creates). If fewer than
    `DEFAULT_RETENTION_THRESHOLD` (50%) of the keywords survive, the
    call returns `{"action": "approve", ...}` so the existing
    `tools.approval.request_tool_approval` gate can prompt the human.
- **post hook (`curator_post_tool_call_hook`)** — writes one JSON
  Lines record per call to the audit log with status, duration, and
  result preview.

The hooks are scoped to the curator LLM pass via a thread-local
context set by `enter_curator_context()` / `exit_curator_context()`,
called from `_run_llm_review` in `agent/curator.py`. Outside that
window the hooks are no-ops, so other agents using the same hook
system are unaffected.

## 2. Verification methodology — three test layers

| Layer | What it tests | Speed | Cost | What it CAN find | What it CANNOT find |
|---|---|---|---|---|---|
| **L1 demo** (`agent/curator_hooks_demo.py`) | Pure hook logic against synthesized data | Seconds | Free | Wrong verdict branches, missing state transitions, CJK tokenisation, register/unregister lifecycle | Anything that depends on the real hook dispatch shim, real SKILL.md structure, real LLM behaviour |
| **L2 real data** (`agent/curator_hooks_real.py`) | Same hook logic against the real `./skills/` tree (73 SKILL.md, 19 skill dirs) | Seconds | Free | Data-driven false positives/negatives: hyphenated names, special characters, sub-skill layout, library structure assumptions | Anything that depends on the real Hermes invoke_hook shim, or the LLM's actual decisions |
| **L3 real LLM** (`run_real_curator_dryrun.py`) | The whole curator pipeline through a forked AIAgent, with MiniMax M3 (or any Anthropic-compatible provider) doing the real LLM work | ~1 minute | API tokens | Hook contract mismatches with the Hermes plugin system, parameter name drift, missing context propagation, LLM-driven scenarios we couldn't anticipate | LLM self-discipline problems (LLM may refuse to call mutating tools under dry-run even when it should) |

**The rule**: every layer catches a *different class* of bug. Skipping
any layer leaves that class un-tested. We learned this the hard way:

- L1 alone → missed the hyphen keyword bug, missed the umbrella
  self-reference design flaw, missed the hook-contract bug.
- L2 alone → would still have missed the hook-contract bug (which is
  a parameter-name mismatch, not a data-shape bug).
- L3 alone → too slow to iterate on (every run costs API tokens and
  takes a minute), and LLM behaviour is variable so the same run
  can produce different verdicts.

L1 is for fast feedback during development. L2 is for "do my
assumptions about the real world hold". L3 is for "is the integration
wired up correctly against the real Hermes runtime". A change should
go through all three before it lands.

## 3. Bugs that were caught — and at which layer

### Bug #1: dry-run is paper-thin by default

**How we found it.** Reading `agent/curator.py:1605-1632` and
`CURATOR_DRY_RUN_BANNER` (line 390). The banner is prompt text — it
tells the LLM "don't mutate, just report". But the LLM is a language
model; nothing structurally prevents it from calling
`skill_manage action=patch` anyway. The original code has zero
enforcement at the tool-dispatch layer. With `consolidate: false`
(the default in `config.yaml`), the LLM pass doesn't even run, so
this is moot for the default install. But once someone flips
`consolidate: true` to opt in, dry-run is *only* a prompt instruction.

**Why the test layers didn't catch it earlier.** This isn't a bug
in the test layers — it's a design flaw in `curator.py` itself. No
amount of unit testing the existing prompt or banner would surface
it; the fix has to be a layer that the LLM can't bypass.

**How we fixed it.** A pre_tool_call hook that intercepts every
mutating `skill_manage` call when the curator context is active and
`dry_run=True`, returning `{"action": "block", "message": ...}`
regardless of what the LLM said in its prompt. Now dry-run is a hard
ceiling, not a polite request.

### Bug #2: hyphenated skill names always fail the keyword retention check

**How we found it.** L2 (real-data) test. We had just shipped the
hook with a working word-boundary regex:

```python
pattern = r"\b" + re.escape(kw) + r"\b"
```

The L1 demo passed cleanly (its synthesised `pr-triage-salvage`
content happened to use the same hyphenated form as the keyword).
Then we ran L2 against the real `./skills/` tree and four
"should-allow" patches all came back `approve_needed` with
`retention_ratio: 0.0`:

| Skill | Content the LLM "wrote" | Why retention hit 0% |
|---|---|---|
| `github-code-review` | "GitHub Code Review\n\n## Review workflow..." | `\bgithub-code-review\b` does NOT match `GitHub Code Review` |
| `claude-code` | "Claude Code\n\n## Delegation workflow..." | Same |
| `github-workflows` | "GitHub Workflows\n\nClass-level umbrella..." | Same |
| `coding-cli-delegation` | "Coding CLI Delegation\n\nDelegate coding tasks..." | Same |

The bug: `\b` is a non-word-character boundary. `-` is a non-word
character, so `\bgithub-code-review\b` requires the literal string
`github-code-review` to appear contiguously. Real human-written and
LLM-written content uses spaces (or capital letters) between the
parts, not hyphens. The keyword exists nowhere in the content, the
match fails, and 0% retention is reported.

**Why L1 didn't catch it.** The L1 demo's synthesised
`pr-triage-salvage` content was written as `PR Triage Salvage` with
spaces — and the original regex *also* failed to match it. But the
demo's other two "should-allow" cases (write_file `ascii-art` and
`delete`) returned `allow` correctly because they didn't actually
rely on the keyword match (write_file had `ascii-art` in its content;
delete was `allow_no_content`). So the bug was *in* the L1 data
without us noticing. The real-data test happened to be the first
time we looked at a content-shape that uses Title Case with spaces.

**How we fixed it.** Split the keyword on `[-_\s]+` and re-join with
`[\s\-_]?` so the name `github-code-review` compiles to
`\bgithub[\s\-_]?code[\s\-_]?review\b`. That matches all four common
spellings (hyphen, underscore, space, mixed case) without matching
`github-codes-review` (the trailing `s` is a real word boundary, not
a separator). See `compute_keyword_retention` in
`agent/curator_hooks.py`.

After the fix, all four "should-allow" cases went from 0% to 100%
retention and the four "should-approve" cases (LLM going off the
rails) still got caught. Zero false negatives introduced.

### Bug #3: umbrella-creation rule is unreasonably strict

**How we found it.** L2 (real-data) test, *after* Bug #2 was fixed.
With hyphenation no longer an issue, the remaining approve_needed
cases were all umbrella creates. The `vague-umbrella` test scenario
specifically:

```yaml
action: create
name: vague-umbrella
file_content: |
  # General Stuff
  A collection of helpful utilities for various tasks.
```

Retention was 0% because the content didn't contain the string
`vague-umbrella` (it didn't contain any skill-name string at all).
The hook escalated to approval. That sounds correct, but then we
tried a *legitimate* umbrella create:

```yaml
action: create
name: github-workflows
file_content: |
  # GitHub Workflows
  Class-level umbrella covering github-code-review,
  github-pr-workflow, github-issues, github-repo-management, ...
```

This is a perfect umbrella: a real LLM curator would emit exactly
this. But the hook also escalated it to approval — the content
mentions the absorbed skills by name but not the new umbrella's
own name. **The rule required the new content to mention the
machine name of the umbrella being created, which is the exact
opposite of what a real LLM would write.** LLMs naturally open an
umbrella with its *title* (`GitHub Workflows`), not its *slug*
(`github-workflows`).

**Why L1 didn't catch it.** The L1 demo didn't include any umbrella
creation scenarios with realistic content. It only tested `create`
for plain new skills, where the `name: foo` self-reference is
obvious. The umbrella self-reference problem is a design
question that only emerges when you write content the way a real
LLM would.

**How we fixed it.** The rule changed from "the new content must
mention the umbrella's own name" to "the new content must mention
at least one of the absorbed skills' names". Concretely, the hook
now reads an optional `merged_skills` (or `absorbed_from`,
`source_skills`, `cluster_members`, `absorbed_skills`) argument from
the call and treats those skills' keywords as a *union* with the
umbrella's own keywords. If the LLM says "I'm absorbing X, Y, Z into
this new umbrella" and writes content that mentions X, the rule
passes.

```python
def _get_keywords_for(state, target, extra_skills=None):
    keywords = list(_load_skill_keywords(target))
    for extra in extra_skills or []:
        if not extra or extra == target:
            continue
        keywords.extend(_load_skill_keywords(extra))
    return deduped_preserving_order(keywords)
```

Validation on 14 real-data operations:

- `vague-umbrella` with NO `merged_skills`, content "General Stuff" → still `approve_needed` (fallback to old rule).
- `vague-umbrella` WITH `merged_skills=[vague-thing, vague-utility]`, content mentioning both → `allow` (new rule fires).
- `vague-umbrella` WITH `merged_skills` but content ignoring them → `approve_needed` (new rule does NOT make the hook loose).

### Bug #4: hook kwargs don't match the Hermes dispatch contract (the one that *should* have been obvious)

**How we found it.** L3 (real LLM) test. After Bugs #2 and #3 were
fixed, the L1 and L2 layers all passed. Then we ran the curator
through a real `AIAgent.run_conversation` against MiniMax M3 and
the agent log immediately filled with this:

```
WARNING hermes_cli.plugins: Hook 'pre_tool_call' callback
  curator_pre_tool_call_hook raised: curator_pre_tool_call_hook()
  missing 1 required keyword-only argument: 'function_name'
```

…and again, for every single tool call the LLM made. The
`PluginManager.invoke_hook` wraps each callback in a try/except and
swallows the TypeError, so the hook looked like it was working —
it just never ran anywhere. Dry-run guard did nothing. Keyword
retention check did nothing. The audit log stayed empty.

The bug: my hook signatures were:

```python
def curator_pre_tool_call_hook(*, function_name, function_args, ...):
def curator_post_tool_call_hook(*, function_name, function_args, ...):
```

But `hermes_cli/plugins.py:2145-2148` calls them as:

```python
invoke_hook("pre_tool_call", tool_name=tool_name, args=args, ...)
```

and `model_tools.py:1005-1008` as:

```python
invoke_hook("post_tool_call", tool_name=function_name, args=function_args, ...)
```

The Hermes convention is `tool_name` and `args`, not `function_name`
and `function_args`. Strict keyword-only parameters + wrong names =
TypeError on every call. L1 and L2 didn't catch this because both
bypass the Hermes `invoke_hook` shim and call the hook function
directly with whatever kwargs they want — name mismatches only
surface when the shim is in the path.

**Why this is the most important bug.** It's the only one that
*only* L3 could have found. L1 and L2 had passed cleanly for days
and we were about to declare the integration done. The only reason
we caught it was a last-minute "let's verify it works in a real
agent" step. If we had skipped L3 and shipped, the hooks would have
been 100% non-functional in production. The try/except wrapper in
`PluginManager.invoke_hook` makes this kind of failure *silent* in
normal use — you'd only notice via the `WARNING` line in the log,
which most users never look at.

**How we fixed it.** Renamed the hook parameters:

```python
def curator_pre_tool_call_hook(*, tool_name, args, ...):
def curator_post_tool_call_hook(*, tool_name, args, ...):
```

…and updated all internal references plus the L1 and L2 demo
callers. The fix is two lines in two files; the bug is that simple
once you see the contract. But the cost of *not* doing the L3 test
would have been shipping broken hooks that look fine in every
other kind of test.

**Defensive addition.** We also added an "observed" verdict
(`verdict: "observed"`) that fires when the curator hook context is
active but the tool is not `skill_manage`. This is purely diagnostic
— it lets us verify the hook is firing inside the real conversation
loop even when the LLM only calls read-only tools like `skill_view`.
Set `CURATOR_HOOK_DEBUG=0` to silence. The first real LLM run after
the fix produced exactly 4 `observed` records (one per `skill_view`
call) — proof the hook is now actually running.

## 4. What L3 *couldn't* verify, and what we did about it

LLM behaviour under `dry_run=True`: the real LLM, presented with
the `CURATOR_DRY_RUN_BANNER`, simply *did not call any mutating
`skill_manage`*. It made 4 `skill_view` calls, evaluated the
candidate list, wrote a report, and exited. So the L3 test
confirmed hook *integration* (4 observed records) but did not
confirm hook *interception* (zero `block_dry_run` records because
the LLM never tried to mutate).

This is a known limitation. A future test could force the LLM to
emit a mutating call (e.g. remove the banner, or use a different
prompt), but that conflicts with the user's requirement to keep
`dry_run=True` for this verification. We accept the limitation:
L1 + L2 already prove the interception logic is correct against
crafted inputs; L3 proves the hook reaches the conversation loop.
Together they cover the integration surface.

## 5. How to use this module

```python
# In a curator LLM pass (agent/curator.py already does this):
from agent import curator_hooks
audit_log = get_hermes_home() / "logs" / "curator" / "audit.jsonl"
curator_hooks.register_curator_hooks()
curator_hooks.enter_curator_context(dry_run=dry_run, audit_log_path=audit_log)
try:
    result = review_agent.run_conversation(user_message=prompt)
finally:
    curator_hooks.exit_curator_context()
    curator_hooks.unregister_curator_hooks()
```

Or, for offline testing of the hook logic alone, the two demo
scripts under `agent/`:

```bash
python -m agent.curator_hooks_demo   # L1: synthesized data, 5 scenarios
python -m agent.curator_hooks_real   # L2: real ./skills/ tree, 14 operations
```

For L3 (real LLM integration), the project root has a one-shot
init script plus the runner:

```bash
# One-time setup (copies 4 real skills into the hermes data dir
# and writes a config.yaml pointing at MiniMax M3):
python init_real_curator_test.py
python init_hermes_config.py

# Run:
python run_real_curator_dryrun.py
# Check the audit log: ~/.hermes/logs/curator/audit.jsonl
```

`run_real_curator_dryrun.py` requires the full hermes runtime
dependencies installed (hermes-agent is `pip install -e .`'d; the
init scripts and the runner do `pip install -e .` to satisfy that
plus the `python-dotenv` / `httpx` / `websockets` /
`concurrent-log-handler` chain that the AIAgent fork imports).

## 6. Audit log format

JSON Lines at `~/.hermes/logs/curator/audit.jsonl`. One record per
`pre_tool_call` or `post_tool_call` that fired inside the curator
context. Fields:

```json
{
  "ts": "2026-07-18T00:53:08.505502+00:00",
  "hook": "pre_tool_call" | "post_tool_call",
  "verdict": "block_dry_run" | "approve_needed" | "allow" |
            "allow_no_target" | "allow_no_content" | "observed",
  "tool": "skill_manage" | "skill_view" | ...,
  "action": "patch" | "create" | "write_file" | "delete" | null,
  "name": "<skill name>" | null,
  "tool_call_id": "chatcmpl-tool-...",
  "session_id": "20260718_085301_a6624a",
  "retention_ratio": 0.62,        // pre hook only
  "preserved": ["github", "code", "review"],
  "missing": ["workflows"],
  "args": { ... },                 // truncated to 200 chars
  "result_preview": "...",         // post hook only
  "duration_ms": 42,               // post hook only
  "status": "ok" | "blocked" | "error" | "cancelled",  // post hook
  "error_type": "plugin_block",    // post hook on failure
  "error_message": "...",
  "message": "[curator-guard] ...", // human-readable explanation
  "rule_key": "curator_guard:foo:patch",  // for the approval allowlist
  "note": "..."                    // for the allow_no_* variants
}
```

## 7. Known limitations and what was *not* fixed

- **CJK keyword matching** uses substring (`kw in new_content`).
  This works for single-character CJK keywords and short compound
  names but is imprecise for longer CJK phrases. A character n-gram
  approach was considered and explicitly deferred — see
  `agent/curator_hooks.py` near the CJK branch in
  `compute_keyword_retention`.
- **LLM self-discipline under dry-run** means the real LLM often
  doesn't call mutating tools when it should. Hook interception
  wasn't verified end-to-end against a real mutating call. The
  four `observed` records in the L3 test are evidence the hook is
  reached, not evidence the hook intercepts. (See §4.)
- **`merged_skills` is opt-in.** The LLM has to pass the argument
  explicitly. There's no enforcement that the absorbed skills in
  the post-hoc `_classify_removed_skills` heuristic match what the
  `create` call declared. If a future LLM driver ever needs the
  hook to *infer* `merged_skills` from prior `delete` calls in the
  same conversation turn, that will need a thread-local buffer
  inside `curator_hooks.py`.
- **Hook is global on the PluginManager singleton.** The
  register/unregister lifecycle must be paired correctly across
  reentrant curator runs. `_run_llm_review` already wraps it in a
  `try/finally`. If you call `register_curator_hooks()` manually
  outside `_run_llm_review`, you must call `unregister_curator_hooks()`
  before the curator process exits or other agents in the same
  Python process will see the curator pre/post hooks fire too.

## 8. Commits in this series

```
0a486b8 fix(agent/curator-hooks): align hook kwargs with Hermes invoke_hook contract
2ec45d4 feat(agent/curator-hooks): accept merged_skills on umbrella create
82f6657 feat(agent/curator-hooks): dry-run hard block, keyword retention gate, and JSONL audit log
```

Reading these three commits in order reproduces the bug-hunt
timeline. Each commit is independent and can be reviewed on its
own; the series is best read in order so you can see the test
data that motivated each fix.
