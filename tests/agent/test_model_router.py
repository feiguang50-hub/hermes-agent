"""Tests for smart model routing (agent/model_router.py + wiring).

All tests are hermetic — the classifier and credential resolution are
stubbed, so nothing hits the network. The invariants under test:

* routing is a strict no-op when disabled (the default),
* routing is Nous-only — a non-Nous session is never touched, and every
  tier resolves through the Nous provider,
* a tier that maps to the current model never triggers a switch
  (the cache-safety guarantee),
* the min_tier floor is honored,
* classification fails open to the default tier,
* explicit delegation/model pins beat routing,
* the session-start helper fires at most once and skips resumed sessions.
"""

import types

import pytest

from agent import model_router
from agent.model_router import RoutingDecision


def _cfg(**routing):
    # Tiers are Nous Portal model ids (smart routing is Nous-only).
    base = {
        "enabled": True,
        "apply_to_sessions": True,
        "apply_to_delegation": True,
        "tiers": {
            "light": "google/gemini-3.5-flash",
            "standard": "",
            "heavy": "anthropic/claude-opus-4.8",
        },
        "default_tier": "standard",
        "min_tier": "",
        "announce": True,
    }
    base.update(routing)
    return {"smart_model_routing": base}


# ── pure helpers ────────────────────────────────────────────────────────


def test_parse_tier_exact_and_embedded():
    assert model_router._parse_tier("heavy", "standard") == "heavy"
    assert model_router._parse_tier("  Light\n", "standard") == "light"
    assert model_router._parse_tier("I think this is standard work", "heavy") == "standard"


def test_parse_tier_fails_open_to_default():
    assert model_router._parse_tier("", "standard") == "standard"
    assert model_router._parse_tier("banana", "heavy") == "heavy"


def test_min_tier_floor_bumps_up():
    cfg = _cfg(min_tier="standard")["smart_model_routing"]
    assert model_router._apply_min_tier_floor("light", cfg) == "standard"
    assert model_router._apply_min_tier_floor("heavy", cfg) == "heavy"


def test_min_tier_floor_ignores_invalid():
    cfg = _cfg(min_tier="bogus")["smart_model_routing"]
    assert model_router._apply_min_tier_floor("light", cfg) == "light"


def test_tier_model_reads_config():
    cfg = _cfg()["smart_model_routing"]
    assert model_router._tier_model("light", cfg) == "google/gemini-3.5-flash"
    assert model_router._tier_model("standard", cfg) == ""


def test_tier_model_accepts_legacy_dict_and_ignores_provider():
    cfg = _cfg(
        tiers={"heavy": {"provider": "anthropic", "model": "anthropic/claude-opus-4.8"}}
    )["smart_model_routing"]
    assert model_router._tier_model("heavy", cfg) == "anthropic/claude-opus-4.8"


def test_is_nous_provider():
    assert model_router._is_nous_provider("nous")
    assert model_router._is_nous_provider("  Nous  ")
    assert not model_router._is_nous_provider("openrouter")
    assert not model_router._is_nous_provider("")


# ── route() behavior ──────────────────────────────────────────────────────


def test_route_disabled_is_noop():
    decision = model_router.route(
        "anything",
        current_model="openai/gpt-5.5",
        current_provider="nous",
        config=_cfg(enabled=False),
    )
    assert decision is None


def test_route_noop_when_not_on_nous(monkeypatch):
    # Nous-only: an enabled router never touches a non-Nous session.
    monkeypatch.setattr(model_router, "classify_complexity", lambda *a, **k: ("heavy", "x"))
    decision = model_router.route(
        "hard refactor",
        current_model="gpt-5.4",
        current_provider="openrouter",
        config=_cfg(),
    )
    assert decision is None


def test_route_tier_with_no_target_stays(monkeypatch):
    # standard tier maps to empty → stay on current model.
    monkeypatch.setattr(model_router, "classify_complexity", lambda *a, **k: ("standard", "x"))
    decision = model_router.route(
        "normal task",
        current_model="openai/gpt-5.5",
        current_provider="nous",
        config=_cfg(),
    )
    assert decision is None


def test_route_noop_when_tier_matches_current(monkeypatch):
    # heavy tier resolves to the model we're already on → no switch (cache-safe).
    monkeypatch.setattr(model_router, "classify_complexity", lambda *a, **k: ("heavy", "x"))
    decision = model_router.route(
        "hard refactor",
        current_model="anthropic/claude-opus-4.8",
        current_provider="nous",
        config=_cfg(),
    )
    assert decision is None


def test_route_returns_decision_on_tier_change(monkeypatch):
    monkeypatch.setattr(model_router, "classify_complexity", lambda *a, **k: ("heavy", "x"))
    monkeypatch.setattr(
        model_router,
        "_resolve_tier_credentials",
        lambda p, m: {"provider": "nous", "model": m,
                      "base_url": "https://inference-api.nousresearch.com/v1",
                      "api_key": "sk", "api_mode": None},
    )
    decision = model_router.route(
        "hard refactor",
        current_model="openai/gpt-5.5",
        current_provider="nous",
        config=_cfg(),
    )
    assert isinstance(decision, RoutingDecision)
    assert decision.tier == "heavy"
    assert decision.model == "anthropic/claude-opus-4.8"
    assert decision.provider == "nous"


def test_route_resolves_tier_against_nous(monkeypatch):
    # The tier model is always resolved through the Nous provider.
    monkeypatch.setattr(model_router, "classify_complexity", lambda *a, **k: ("light", "x"))
    captured = {}

    def _fake_resolve(provider, model):
        captured["provider"] = provider
        captured["model"] = model
        return {"provider": provider, "model": model, "base_url": None,
                "api_key": "sk", "api_mode": None}

    monkeypatch.setattr(model_router, "_resolve_tier_credentials", _fake_resolve)
    decision = model_router.route(
        "tiny edit",
        current_model="openai/gpt-5.5",
        current_provider="nous",
        config=_cfg(),
    )
    assert decision is not None
    assert captured["provider"] == model_router.NOUS_PROVIDER
    assert captured["model"] == "google/gemini-3.5-flash"


def test_route_fails_open_when_credentials_unresolved(monkeypatch):
    monkeypatch.setattr(model_router, "classify_complexity", lambda *a, **k: ("light", "x"))
    monkeypatch.setattr(model_router, "_resolve_tier_credentials", lambda p, m: None)
    decision = model_router.route(
        "tiny edit",
        current_model="openai/gpt-5.5",
        current_provider="nous",
        config=_cfg(),
    )
    assert decision is None


def test_route_honors_min_tier(monkeypatch):
    # classifier says light, but min_tier=heavy forces heavy.
    monkeypatch.setattr(model_router, "classify_complexity", lambda *a, **k: ("light", "x"))
    captured = {}

    def _fake_resolve(provider, model):
        captured["provider"] = provider
        captured["model"] = model
        return {"provider": provider, "model": model, "base_url": None,
                "api_key": "sk", "api_mode": None}

    monkeypatch.setattr(model_router, "_resolve_tier_credentials", _fake_resolve)
    decision = model_router.route(
        "tiny edit",
        current_model="openai/gpt-5.5",
        current_provider="nous",
        config=_cfg(min_tier="heavy"),
    )
    assert decision is not None
    assert decision.tier == "heavy"
    assert captured["model"] == "anthropic/claude-opus-4.8"


# ── classify_complexity fail-open ─────────────────────────────────────────


def test_classify_fails_open_without_aux_client(monkeypatch):
    import agent.auxiliary_client as aux

    monkeypatch.setattr(aux, "get_text_auxiliary_client", lambda task: (None, None))
    tier, reason = model_router.classify_complexity(
        "do something", routing_cfg=_cfg()["smart_model_routing"]
    )
    assert tier == "standard"
    assert "no auxiliary client" in reason


def test_classify_empty_message_returns_default():
    tier, reason = model_router.classify_complexity(
        "   ", routing_cfg=_cfg(default_tier="heavy")["smart_model_routing"]
    )
    assert tier == "heavy"


# ── session-start wiring (_maybe_apply_session_routing) ───────────────────


class _FakeAgent:
    def __init__(self):
        self.model = "openai/gpt-5.5"
        self.provider = "nous"
        self.quiet_mode = True
        self.switched = None
        self._smart_routing_applied = False

    def switch_model(self, **kwargs):
        self.switched = kwargs
        self.model = kwargs["new_model"]
        self.provider = kwargs["new_provider"]


def test_session_routing_skips_resumed_session(monkeypatch):
    from agent import conversation_loop

    agent = _FakeAgent()
    # Non-empty history → must not classify or switch, but must mark applied.
    conversation_loop._maybe_apply_session_routing(agent, "hi", [{"role": "user", "content": "x"}])
    assert agent.switched is None
    assert agent._smart_routing_applied is True


def test_session_routing_applies_once_and_switches(monkeypatch):
    from agent import conversation_loop

    agent = _FakeAgent()
    monkeypatch.setattr(model_router, "get_routing_config", lambda config=None: _cfg()["smart_model_routing"])
    monkeypatch.setattr(
        model_router,
        "route",
        lambda *a, **k: RoutingDecision(
            tier="heavy", provider="nous", model="anthropic/claude-opus-4.8",
            base_url=None, api_key="sk", api_mode=None, reason="classified",
        ),
    )
    conversation_loop._maybe_apply_session_routing(agent, "hard task", None)
    assert agent.switched is not None
    assert agent.model == "anthropic/claude-opus-4.8"
    assert agent._smart_routing_applied is True

    # Second call must be a no-op (flag already set).
    agent.switched = None
    conversation_loop._maybe_apply_session_routing(agent, "another", None)
    assert agent.switched is None


# ── delegation wiring (_route_task_creds) ─────────────────────────────────


def test_delegation_routing_respects_explicit_model():
    from tools import delegate_tool

    base = {"model": "pinned/model", "provider": "nous", "base_url": None,
            "api_key": None, "api_mode": None}
    parent = types.SimpleNamespace(model="openai/gpt-5.5", provider="nous")
    out = delegate_tool._route_task_creds(base, "anything", parent)
    assert out is base  # unchanged — explicit delegation.model wins


def test_delegation_routing_sets_model_when_unpinned(monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr(model_router, "get_routing_config", lambda config=None: _cfg()["smart_model_routing"])
    monkeypatch.setattr(
        model_router,
        "route",
        lambda *a, **k: RoutingDecision(
            tier="light", provider="nous", model="google/gemini-3.5-flash",
            base_url=None, api_key="sk", api_mode=None, reason="classified",
        ),
    )
    base = {"model": None, "provider": None, "base_url": None, "api_key": None, "api_mode": None}
    parent = types.SimpleNamespace(model="openai/gpt-5.5", provider="nous")
    out = delegate_tool._route_task_creds(base, "tiny task", parent)
    assert out["model"] == "google/gemini-3.5-flash"
    assert out["provider"] == "nous"


def test_delegation_routing_noop_returns_base(monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr(model_router, "get_routing_config", lambda config=None: _cfg()["smart_model_routing"])
    monkeypatch.setattr(model_router, "route", lambda *a, **k: None)
    base = {"model": None, "provider": None, "base_url": None, "api_key": None, "api_mode": None}
    parent = types.SimpleNamespace(model="openai/gpt-5.5", provider="nous")
    out = delegate_tool._route_task_creds(base, "task", parent)
    assert out is base
