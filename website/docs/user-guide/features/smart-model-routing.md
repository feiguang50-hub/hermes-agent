---
title: Smart Model Routing
description: Auto-pick a tier-appropriate model per request without breaking your prompt cache.
sidebar_label: Smart Model Routing
sidebar_position: 9
---

# Smart Model Routing

Smart model routing is Hermes' take on a Cursor-style **"Auto"** model picker:
a cheap classifier reads an incoming request, labels how much capability it
needs (`light` / `standard` / `heavy`), and runs it on a model you've mapped to
that tier. Hard tasks get a frontier model; trivial ones get something small
and fast.

It is a **Nous Portal feature**: every tier runs on the
[Nous Portal](https://portal.nousresearch.com), which fronts the frontier
models across vendors (`anthropic/…`, `openai/…`, `google/…`, `x-ai/…`) behind
a single credential — so one Portal key covers every tier. Routing only
engages when your active model is itself on the Nous Portal; if you're on any
other provider it stays out of the way entirely (it never moves you onto Nous).

It is **off by default**, and when on it is **prompt-cache-safe by design**.

## When does it route?

This is the part that makes it different from a naive "switch the model every
turn" router. Hermes' per-conversation prompt caching is
sacred: swapping the main model mid-conversation throws away the cached prefix
and re-pays full input price on the new model — which, in a long thread, can
cost *more* than it saves. So routing only ever happens where there is **no
cached prefix to invalidate**:

| Where | What it does | Cache impact |
|-------|--------------|--------------|
| **Session start** | Classifies the first message of a *fresh* session and picks the model **before the first API call**. | None — nothing is cached yet. |
| **Delegation** | Classifies each `delegate_task` subtask's goal and picks the subagent's model. | None — subagents start from fresh context. |

It does **not** swap your main model mid-conversation. That remains the job of
the explicit [`/model`](../../reference/slash-commands.md) command (which
deliberately resets the cache). Resumed sessions are never re-routed.

## Prerequisites

You need the **Nous Portal** configured as a provider (`hermes auth add nous`
or `hermes model` → Nous Portal) and your main model running on it. Routing is
Nous-only — it stays inert on every other provider.

## Enabling it

Add a `smart_model_routing` block to `~/.hermes/config.yaml`. Tiers are just
Nous Portal model ids:

```yaml
smart_model_routing:
  enabled: true
  apply_to_sessions: true     # route at the start of a fresh session
  apply_to_delegation: true   # route delegated subtasks by their goal
  tiers:
    light: google/gemini-3.5-flash     # fast + cheap
    standard: ""                       # empty = stay on your main model
    heavy: anthropic/claude-opus-4.8   # frontier
  default_tier: standard      # used when the classifier can't be reached
  min_tier: ""                # set to "standard" to forbid the light tier
  announce: true              # print the routing decision

# Point the picker at a small, fast Portal model — it runs once per fresh
# session and per delegated subtask, so an expensive classifier defeats the
# purpose.
auxiliary:
  routing_classifier:
    provider: nous
    model: google/gemini-3.5-flash
```

### Tiers

There are three ordered tiers — `light`, `standard`, `heavy`. Each maps to a
**Nous Portal model id**; the provider is always Nous, so you only specify the
model. Credentials (`base_url`, `api_key`, `api_mode`) resolve automatically
from your Nous credential, exactly like [`delegation.model`](./delegation.md).
Leave a tier empty to mean **"stay on the current/parent model"** — that's the
natural baseline for `standard`.

### The classifier

The picker runs through the `auxiliary.routing_classifier` task (see
[Auxiliary Models](../configuration.md#auxiliary-models)). It sends the request
to the configured Portal model and asks for a one-word tier label. It is
**fail-open**: if the classifier is unreachable, slow, or returns garbage,
Hermes falls back to `default_tier` and never wedges your turn.

## Tuning

- **`min_tier`** is a quality-first guardrail. Set it to `standard` to forbid
  the `light` tier entirely, so the router can upgrade but never downgrade below
  your floor. Empty means no floor.
- **`default_tier`** is where requests land when classification fails. Keep it
  at `standard` (or higher) so a flaky classifier degrades toward quality.
- The classifier is told to **bias toward the higher tier when unsure** — the
  most common complaint about auto-routers is picking a weak model for a hard
  task, so the default leans conservative.

## Relationship to other model controls

| Feature | What it controls |
|---------|------------------|
| `smart_model_routing` | Auto-picks a tier model at session start / delegation. |
| [`/model`](../../reference/slash-commands.md) | Manual, explicit switch for the current session (always wins; resets cache). |
| [`fallback_providers`](./fallback-providers.md) | Failover when a model **errors** (rate limit, outage) — not task-based. |
| [`delegation.model`](./delegation.md) | Pins a fixed model for all subagents. An explicit pin **beats** routing. |

An explicit `delegation.model` always wins over delegation routing, and an
explicit `/model` always wins over session routing.
