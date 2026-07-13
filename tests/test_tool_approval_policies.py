from __future__ import annotations

import contextvars
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _no_plugin_execution_middleware(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.middleware._get_middleware_callbacks", lambda _kind: []
    )


def test_policy_resolves_tool_name_before_toolset_and_uses_most_restrictive(monkeypatch):
    from hermes_cli import middleware

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "approvals": {
                "tool_policies": {
                    "file*": "allow",
                    "write_*": "ask",
                    "write_file": "deny",
                }
            }
        },
    )
    entry = SimpleNamespace(toolset="files")

    assert middleware.resolve_tool_approval_policy("write_file", entry=entry) == "deny"


def test_broad_deny_outweighs_exact_allow(monkeypatch):
    from hermes_cli import middleware

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "approvals": {
                "tool_policies": {
                    "*": "deny",
                    "read_file": "allow",
                }
            }
        },
    )

    assert middleware.resolve_tool_approval_policy("read_file") == "deny"


def test_toolset_deny_outweighs_tool_name_allow(monkeypatch):
    from hermes_cli import middleware

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "approvals": {
                "tool_policies": {
                    "read_file": "allow",
                    "file": "deny",
                }
            }
        },
    )
    entry = SimpleNamespace(toolset="file")

    assert middleware.resolve_tool_approval_policy("read_file", entry=entry) == "deny"


def test_policy_can_match_registered_toolset(monkeypatch):
    from hermes_cli import middleware

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"approvals": {"tool_policies": {"browser*": "ask"}}},
    )
    entry = SimpleNamespace(toolset="browser")

    assert middleware.resolve_tool_approval_policy("browser_click", entry=entry) == "ask"


def test_malformed_policy_entries_are_ignored(monkeypatch):
    from hermes_cli import middleware

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "approvals": {
                "tool_policies": {
                    "terminal": "maybe",
                    42: "deny",
                    "*": None,
                }
            }
        },
    )

    assert middleware.resolve_tool_approval_policy("terminal") is None


def test_deny_policy_blocks_without_calling_tool(monkeypatch):
    from hermes_cli import middleware

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "deny"
    )
    called = []

    result = middleware.run_tool_execution_middleware(
        "write_file", {"path": "notes.txt"}, lambda args: called.append(args)
    )

    assert called == []
    assert json.loads(result)["error"].startswith("BLOCKED: Tool 'write_file'")


def test_ask_policy_uses_shared_fail_closed_approval_gate(monkeypatch):
    from hermes_cli import middleware

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "ask"
    )
    requested = []
    monkeypatch.setattr(
        "tools.approval.request_tool_approval",
        lambda tool_name, reason, **kwargs: requested.append(
            (tool_name, reason, kwargs)
        )
        or {"approved": False, "message": "BLOCKED by cron policy"},
    )
    called = []

    result = middleware.run_tool_execution_middleware(
        "terminal", {"command": "printf ok"}, lambda args: called.append(args)
    )

    assert json.loads(result)["error"] == "BLOCKED by cron policy"
    assert called == []
    assert requested[0][0] == "terminal"
    assert requested[0][2]["rule_key"] == "tool_policy:terminal"


def test_allow_policy_does_not_skip_downstream_execution_middleware(monkeypatch):
    from hermes_cli import middleware

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "allow"
    )
    events = []

    def plugin_middleware(**kwargs):
        events.append("plugin")
        return kwargs["next_call"](kwargs["args"])

    monkeypatch.setattr(
        middleware, "_get_middleware_callbacks", lambda _kind: [plugin_middleware]
    )

    result = middleware.run_tool_execution_middleware(
        "read_file", {"path": "README.md"}, lambda _args: events.append("tool") or "ok"
    )

    assert result == "ok"
    assert events == ["plugin", "tool"]


def test_direct_registry_dispatch_is_policy_gated(monkeypatch):
    import model_tools
    from hermes_cli import middleware

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "deny"
    )
    dispatched = []
    monkeypatch.setattr(
        model_tools.registry,
        "dispatch",
        lambda name, args, **kwargs: dispatched.append((name, args)) or "ran",
    )

    result = model_tools.handle_function_call("read_file", {"path": "README.md"})

    assert dispatched == []
    assert "Tool 'read_file' is denied" in json.loads(result)["error"]


def test_registry_dispatch_itself_is_policy_gated(monkeypatch):
    from hermes_cli import middleware
    from tools.registry import ToolRegistry

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "deny"
    )
    called = []
    registry = ToolRegistry()
    registry.register(
        name="plugin_tool",
        toolset="plugin",
        schema={},
        handler=lambda args, **kwargs: called.append(args) or '{"ran": true}',
    )

    result = registry.dispatch("plugin_tool", {"value": 1})

    assert called == []
    assert "Tool 'plugin_tool' is denied" in json.loads(result)["error"]


def test_custom_registry_dispatch_resolves_policy_from_its_own_toolset(monkeypatch):
    from tools.registry import ToolRegistry

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"approvals": {"tool_policies": {"custom-toolset": "deny"}}},
    )
    called = []
    custom_registry = ToolRegistry()
    custom_registry.register(
        name="shared_name",
        toolset="custom-toolset",
        schema={},
        handler=lambda args, **kwargs: called.append(args) or "ran",
    )

    result = custom_registry.dispatch("shared_name", {})

    assert called == []
    assert json.loads(result)["policy"] == "deny"


def test_genuine_recursive_same_name_dispatch_rechecks_policy_and_middleware(monkeypatch):
    from hermes_cli import middleware
    from tools.registry import ToolRegistry

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "ask"
    )
    approvals = []
    monkeypatch.setattr(
        "tools.approval.request_tool_approval",
        lambda *_args, **_kwargs: approvals.append(True)
        or {"approved": True, "message": None},
    )
    executions = []

    def execution_middleware(**kwargs):
        executions.append(kwargs["args"]["depth"])
        return kwargs["next_call"](kwargs["args"])

    monkeypatch.setattr(
        middleware, "_get_middleware_callbacks", lambda _kind: [execution_middleware]
    )
    custom_registry = ToolRegistry()

    def recursive_handler(args, **_kwargs):
        if args["depth"]:
            return custom_registry.dispatch("recursive", {"depth": args["depth"] - 1})
        return '{"done": true}'

    custom_registry.register("recursive", "custom", {}, recursive_handler)

    assert json.loads(custom_registry.dispatch("recursive", {"depth": 1})) == {"done": True}
    assert approvals == [True, True]
    assert executions == [1, 0]


def test_wrapper_to_registry_dispatch_is_deduped_exactly_once(monkeypatch):
    from hermes_cli import middleware
    from tools.registry import ToolRegistry

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "ask"
    )
    approvals = []
    monkeypatch.setattr(
        "tools.approval.request_tool_approval",
        lambda *_args, **_kwargs: approvals.append(True)
        or {"approved": True, "message": None},
    )
    executions = []

    def execution_middleware(**kwargs):
        executions.append(kwargs["tool_name"])
        return kwargs["next_call"](kwargs["args"])

    monkeypatch.setattr(
        middleware, "_get_middleware_callbacks", lambda _kind: [execution_middleware]
    )
    custom_registry = ToolRegistry()
    custom_registry.register("wrapped", "custom", {}, lambda _args, **_kwargs: "ok")
    args = {"value": 1}

    result = middleware.run_tool_execution_middleware(
        "wrapped",
        args,
        lambda next_args: custom_registry.dispatch("wrapped", next_args),
        dispatch_registry=custom_registry,
    )

    assert result == "ok"
    assert approvals == [True]
    assert executions == ["wrapped"]


def test_copied_wrapper_context_cannot_reuse_consumed_registry_handoff(monkeypatch):
    from hermes_cli import middleware
    from tools.registry import ToolRegistry

    monkeypatch.setattr(middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "ask")
    approvals = []
    monkeypatch.setattr(
        "tools.approval.request_tool_approval",
        lambda *_args, **_kwargs: approvals.append(True) or {"approved": True, "message": None},
    )
    custom_registry = ToolRegistry()
    custom_registry.register("wrapped", "custom", {}, lambda _args, **_kwargs: "ok")
    args = {"value": 1}
    copied = None

    def dispatch_and_copy(next_args):
        nonlocal copied
        copied = contextvars.copy_context()
        return custom_registry.dispatch("wrapped", next_args)

    assert middleware.run_tool_execution_middleware(
        "wrapped", args, dispatch_and_copy, dispatch_registry=custom_registry
    ) == "ok"
    assert copied is not None
    assert copied.run(custom_registry.dispatch, "wrapped", args) == "ok"
    assert approvals == [True, True]


def test_one_wrapper_handoff_cannot_suppress_two_same_argument_dispatches(monkeypatch):
    from hermes_cli import middleware
    from tools.registry import ToolRegistry

    monkeypatch.setattr(middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "ask")
    approvals = []
    monkeypatch.setattr(
        "tools.approval.request_tool_approval",
        lambda *_args, **_kwargs: approvals.append(True) or {"approved": True, "message": None},
    )
    custom_registry = ToolRegistry()
    custom_registry.register("wrapped", "custom", {}, lambda _args, **_kwargs: "ok")
    args = {"value": 1}

    def dispatch_twice(next_args):
        custom_registry.dispatch("wrapped", next_args)
        return custom_registry.dispatch("wrapped", next_args)

    assert middleware.run_tool_execution_middleware(
        "wrapped", args, dispatch_twice, dispatch_registry=custom_registry
    ) == "ok"
    assert approvals == [True, True]


def test_wrapper_dedupe_does_not_cover_a_different_tool_name(monkeypatch):
    from hermes_cli import middleware
    from tools.registry import ToolRegistry

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "ask"
    )
    approvals = []
    monkeypatch.setattr(
        "tools.approval.request_tool_approval",
        lambda *_args, **_kwargs: approvals.append(True)
        or {"approved": True, "message": None},
    )
    custom_registry = ToolRegistry()
    custom_registry.register("outer", "custom", {}, lambda _args, **_kwargs: "unused")
    custom_registry.register("inner", "custom", {}, lambda _args, **_kwargs: "ok")
    args = {"value": 1}

    result = middleware.run_tool_execution_middleware(
        "outer",
        args,
        lambda next_args: custom_registry.dispatch("inner", next_args),
        dispatch_registry=custom_registry,
    )

    assert result == "ok"
    assert approvals == [True, True]


def test_wrapper_registry_dedupe_tokens_are_isolated_across_concurrent_contexts(monkeypatch):
    from hermes_cli import middleware
    from tools.registry import ToolRegistry

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "ask"
    )
    approvals = []
    lock = Lock()

    def approve(*_args, **_kwargs):
        with lock:
            approvals.append(True)
        return {"approved": True, "message": None}

    monkeypatch.setattr("tools.approval.request_tool_approval", approve)
    executions = []

    def execution_middleware(**kwargs):
        with lock:
            executions.append(kwargs["args"]["call"])
        return kwargs["next_call"](kwargs["args"])

    monkeypatch.setattr(
        middleware, "_get_middleware_callbacks", lambda _kind: [execution_middleware]
    )
    barrier = Barrier(2)
    custom_registry = ToolRegistry()

    def handler(args, **_kwargs):
        barrier.wait(timeout=5)
        return str(args["call"])

    custom_registry.register("concurrent", "custom", {}, handler)

    def invoke(call):
        args = {"call": call}
        return middleware.run_tool_execution_middleware(
            "concurrent",
            args,
            lambda next_args: custom_registry.dispatch("concurrent", next_args),
            dispatch_registry=custom_registry,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, (1, 2)))

    assert results == ["1", "2"]
    assert len(approvals) == 2
    assert sorted(executions) == [1, 2]


def test_plugin_context_dispatch_cannot_bypass_policy(monkeypatch):
    from hermes_cli import middleware
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    from tools.registry import registry

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "deny"
    )
    called = []
    registry.register(
        name="plugin_context_policy_test",
        toolset="plugin-test",
        schema={},
        handler=lambda args, **kwargs: called.append(args) or '{"ran": true}',
    )
    try:
        context = PluginContext(
            PluginManifest(name="policy-test", source="user"), PluginManager()
        )
        result = context.dispatch_tool("plugin_context_policy_test", {})
    finally:
        registry.deregister("plugin_context_policy_test")

    assert called == []
    assert "is denied" in json.loads(result)["error"]


def test_nested_model_and_registry_dispatch_ask_only_once(monkeypatch):
    import model_tools
    from hermes_cli import middleware

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "ask"
    )
    approvals = []
    monkeypatch.setattr(
        "tools.approval.request_tool_approval",
        lambda *_args, **_kwargs: approvals.append(True)
        or {"approved": True, "message": None},
    )

    result = model_tools.handle_function_call(
        "read_file", {"path": "README.md"}, skip_pre_tool_call_hook=True
    )

    assert json.loads(result).get("error") is None
    assert approvals == [True]


def test_nested_model_and_registry_dispatch_runs_execution_middleware_once(monkeypatch):
    import model_tools
    from hermes_cli import middleware

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: None
    )
    executions = []

    def execution_middleware(**kwargs):
        executions.append(kwargs["tool_name"])
        return kwargs["next_call"](kwargs["args"])

    monkeypatch.setattr(
        middleware,
        "_get_middleware_callbacks",
        lambda _kind: [execution_middleware],
    )

    result = model_tools.handle_function_call(
        "read_file", {"path": "README.md"}, skip_pre_tool_call_hook=True
    )

    assert json.loads(result).get("error") is None
    assert executions == ["read_file"]


def test_sequential_agent_level_dispatch_is_policy_gated(monkeypatch):
    from agent import tool_executor
    from hermes_cli import middleware

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "deny"
    )
    called = []
    agent = SimpleNamespace(
        session_id="s",
        _current_turn_id="t",
        _current_api_request_id="r",
    )

    result, _ = tool_executor._run_agent_tool_execution_middleware(
        agent,
        function_name="todo",
        function_args={"todos": []},
        effective_task_id="task",
        tool_call_id="call",
        execute=lambda args: called.append(args) or "ran",
    )

    assert called == []
    assert "Tool 'todo' is denied" in json.loads(result)["error"]


def test_concurrent_agent_level_dispatch_is_policy_gated(monkeypatch):
    from agent import agent_runtime_helpers
    from hermes_cli import middleware

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "deny"
    )
    called = []
    agent = SimpleNamespace(
        session_id="s",
        _current_turn_id="t",
        _current_api_request_id="r",
        _todo_store=object(),
        _memory_manager=None,
        valid_tool_names=set(),
        enabled_toolsets=None,
        disabled_toolsets=None,
    )
    monkeypatch.setattr(
        "tools.todo_tool.todo_tool", lambda **kwargs: called.append(kwargs) or "ran"
    )

    result = agent_runtime_helpers.invoke_tool(
        agent, "todo", {"todos": []}, "task", tool_call_id="call",
        pre_tool_block_checked=True,
    )

    assert called == []
    assert "Tool 'todo' is denied" in json.loads(result)["error"]


def test_runtime_fallback_registry_runs_policy_and_execution_middleware_once(monkeypatch):
    """The real invoke_tool fallback must hand off directly to the registry once."""
    import model_tools
    from agent import agent_runtime_helpers
    from hermes_cli import middleware

    approvals = []
    executions = []
    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "ask"
    )
    monkeypatch.setattr(
        "tools.approval.request_tool_approval",
        lambda *_args, **_kwargs: approvals.append(True)
        or {"approved": True, "message": None},
    )

    def execution_middleware(**kwargs):
        executions.append(kwargs["tool_name"])
        return kwargs["next_call"](kwargs["args"])

    monkeypatch.setattr(
        middleware, "_get_middleware_callbacks", lambda _kind: [execution_middleware]
    )
    tool_name = "runtime_fallback_policy_probe"
    model_tools.registry.register(
        tool_name, "probe", {}, lambda _args, **_kwargs: '{"success": true}'
    )
    agent = SimpleNamespace(
        session_id="s",
        _current_turn_id="t",
        _current_api_request_id="r",
        _memory_manager=None,
        _context_engine_tool_names=set(),
        valid_tool_names={tool_name},
        enabled_toolsets=None,
        disabled_toolsets=None,
    )
    try:
        result = agent_runtime_helpers.invoke_tool(
            agent, tool_name, {}, "task", tool_call_id="call", pre_tool_block_checked=True
        )
    finally:
        model_tools.registry.deregister(tool_name)

    assert json.loads(result) == {"success": True}
    assert approvals == [True]
    assert executions == [tool_name]


@pytest.mark.parametrize(
    ("family", "tool_name", "toolset"),
    [
        ("context", "context_policy_probe", "context_engine"),
        ("memory", "memory_policy_probe", "memory"),
    ],
)
def test_dynamic_tool_family_deny_overrides_exact_allow_on_runtime_path(
    monkeypatch, family, tool_name, toolset
):
    """Dynamic schemas carry logical toolset identity without global registration."""
    from agent import agent_runtime_helpers

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "approvals": {
                "tool_policies": {tool_name: "allow", toolset: "deny"}
            }
        },
    )
    from tools.mcp_tool import _reinject_post_build_tools

    calls = []
    schema = {"name": tool_name, "description": "probe", "parameters": {}}
    memory_manager = SimpleNamespace(
        has_tool=lambda name: family == "memory" and name == tool_name,
        handle_tool_call=lambda name, args: calls.append((name, args)) or "ran",
        get_all_tool_schemas=lambda: [schema] if family == "memory" else [],
    )
    context_engine = SimpleNamespace(
        handle_tool_call=lambda name, args, **kwargs: calls.append((name, args)) or "ran",
        get_tool_schemas=lambda: [schema] if family == "context" else [],
    )
    dynamic_entries = {}
    agent = SimpleNamespace(
        session_id="s",
        _current_turn_id="t",
        _current_api_request_id="r",
        _memory_manager=memory_manager,
        context_compressor=context_engine,
        enabled_toolsets=None,
        disabled_toolsets=None,
    )
    tools = []
    names = set()
    engine_names = _reinject_post_build_tools(
        agent, tools, names, dynamic_entries=dynamic_entries
    )
    agent._context_engine_tool_names = engine_names
    agent._dynamic_tool_entries = dynamic_entries
    agent.valid_tool_names = names

    assert dynamic_entries[tool_name].toolset == toolset
    result = agent_runtime_helpers.invoke_tool(
        agent, tool_name, {}, "task", tool_call_id="call", pre_tool_block_checked=True
    )

    assert calls == []
    assert json.loads(result)["policy"] == "deny"


@pytest.mark.parametrize(
    "tool_name,toolset", [("read_terminal", "terminal"), ("delegate_task", "delegation")]
)
def test_sequential_fast_paths_apply_registered_toolset_policy(monkeypatch, tool_name, toolset):
    import model_tools
    from agent import tool_executor

    entry = model_tools.registry.get_entry(tool_name)
    assert entry is not None and entry.toolset == toolset
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"approvals": {"tool_policies": {tool_name: "allow", toolset: "deny"}}},
    )
    called = []
    agent = SimpleNamespace(session_id="s", _current_turn_id="t", _current_api_request_id="r")

    result, _ = tool_executor._run_agent_tool_execution_middleware(
        agent,
        function_name=tool_name,
        function_args={},
        effective_task_id="task",
        tool_call_id="call",
        execute=lambda args: called.append(args) or "ran",
    )

    assert called == []
    assert json.loads(result)["policy"] == "deny"


@pytest.mark.parametrize(
    "tool_name,toolset", [("read_terminal", "terminal"), ("delegate_task", "delegation")]
)
def test_concurrent_fast_paths_apply_registered_toolset_policy(monkeypatch, tool_name, toolset):
    import model_tools
    from agent import agent_runtime_helpers

    entry = model_tools.registry.get_entry(tool_name)
    assert entry is not None and entry.toolset == toolset
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"approvals": {"tool_policies": {tool_name: "allow", toolset: "deny"}}},
    )
    called = []
    agent = SimpleNamespace(
        session_id="s",
        _current_turn_id="t",
        _current_api_request_id="r",
        _memory_manager=None,
        valid_tool_names=set(),
        enabled_toolsets=None,
        disabled_toolsets=None,
        read_terminal_callback=lambda *_a: called.append("read"),
        _dispatch_delegate_task=lambda args: called.append(args) or "ran",
    )

    result = agent_runtime_helpers.invoke_tool(
        agent, tool_name, {}, "task", tool_call_id="call", pre_tool_block_checked=True
    )

    assert called == []
    assert json.loads(result)["policy"] == "deny"


def test_explicit_allow_does_not_bypass_plugin_escalation(monkeypatch):
    import model_tools
    from hermes_cli import middleware

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "allow"
    )
    monkeypatch.setattr(
        "hermes_cli.plugins.resolve_pre_tool_block",
        lambda *_args, **_kwargs: "plugin requires approval",
    )
    dispatched = []
    monkeypatch.setattr(
        model_tools.registry,
        "dispatch",
        lambda name, args, **kwargs: dispatched.append((name, args)) or "ran",
    )

    result = model_tools.handle_function_call("read_file", {"path": "README.md"})

    assert dispatched == []
    assert json.loads(result)["error"] == "plugin requires approval"


def test_explicit_allow_does_not_bypass_terminal_hardline(monkeypatch):
    from hermes_cli import middleware
    from tools.approval import check_all_command_guards

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "allow"
    )
    called = []

    result = middleware.run_tool_execution_middleware(
        "terminal",
        {"command": "rm -rf /"},
        lambda args: called.append(args)
        or check_all_command_guards(args["command"], "local"),
    )

    assert called == [{"command": "rm -rf /"}]
    assert result["approved"] is False
    assert "hardline" in result["message"].lower()


def test_explicit_allow_bypasses_only_ordinary_terminal_prompt(monkeypatch):
    from hermes_cli import middleware
    from tools import approval

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "allow"
    )
    prompted = []

    result = middleware.run_tool_execution_middleware(
        "terminal",
        {"command": "rm -rf /tmp/hermes-policy-test"},
        lambda args: approval.check_all_command_guards(
            args["command"],
            "local",
            approval_callback=lambda *_args, **_kwargs: prompted.append(True) or "deny",
        ),
    )

    assert result["approved"] is True
    assert prompted == []


def test_explicit_allow_does_not_bypass_terminal_user_deny(monkeypatch):
    from hermes_cli import middleware
    from tools import approval

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "allow"
    )
    monkeypatch.setattr(approval, "_match_user_deny_rule", lambda _command: "git push *")

    result = middleware.run_tool_execution_middleware(
        "terminal",
        {"command": "git push origin main"},
        lambda args: approval.check_all_command_guards(args["command"], "local"),
    )

    assert result["approved"] is False
    assert "user-defined deny rule" in result["message"].lower()


def test_explicit_allow_does_not_bypass_credential_path_guard(monkeypatch, tmp_path):
    from hermes_cli import middleware
    from tools.file_tools import write_file_tool

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "allow"
    )
    profile = tmp_path / ".hermes"
    profile.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile))

    result = middleware.run_tool_execution_middleware(
        "write_file",
        {"path": str(profile / ".env"), "content": "SECRET=value"},
        lambda args: write_file_tool(**args),
    )

    assert not (profile / ".env").exists()
    assert "write denied" in json.loads(result)["error"].lower()


def test_ask_policy_honors_cron_deny(monkeypatch):
    from hermes_cli import middleware

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "ask"
    )
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.setattr("tools.approval._get_cron_approval_mode", lambda: "deny")
    called = []

    result = middleware.run_tool_execution_middleware(
        "read_file", {"path": "README.md"}, lambda args: called.append(args)
    )

    assert called == []
    assert "cron jobs run without a user present" in json.loads(result)["error"]


def test_ask_policy_is_not_bypassed_by_session_yolo(monkeypatch):
    from hermes_cli import middleware
    from tools import approval

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name, **_kwargs: "ask"
    )
    monkeypatch.setattr(approval, "is_current_session_yolo_enabled", lambda: True)
    monkeypatch.setattr(approval, "_is_interactive_cli", lambda: True)
    monkeypatch.setattr(approval, "prompt_dangerous_approval", lambda *_a, **_kw: "deny")
    called = []

    result = middleware.run_tool_execution_middleware(
        "write_file", {"path": "notes.txt"}, lambda args: called.append(args)
    )

    assert called == []
    assert "User denied" in json.loads(result)["error"]


def test_default_config_exposes_empty_profile_local_policy_mapping():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["approvals"]["tool_policies"] == {}


def test_reloading_allowlist_replaces_previous_profiles_state(monkeypatch):
    from tools import approval

    configs = iter(
        [
            {"command_allowlist": ["default-only"]},
            {"command_allowlist": ["work-only"]},
        ]
    )
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: next(configs))
    monkeypatch.setattr(approval, "_permanent_approved", set())

    approval.load_permanent_allowlist()
    assert approval.is_approved("session", "default-only") is True

    approval.load_permanent_allowlist()
    assert approval.is_approved("session", "default-only") is False
    assert approval.is_approved("session", "work-only") is True


def test_failed_profile_allowlist_load_clears_previous_profile_state(monkeypatch):
    from tools import approval

    configs = iter([{"command_allowlist": ["default-only"]}, ValueError("malformed profile")])

    def load_config():
        value = next(configs)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr("hermes_cli.config.load_config", load_config)
    monkeypatch.setattr(approval, "_permanent_approved", set())
    approval.load_permanent_allowlist()
    assert approval.is_approved("session", "default-only") is True

    assert approval.load_permanent_allowlist() == set()
    assert approval.is_approved("session", "default-only") is False
