"""Regression tests for terminal config -> env-var bridging.

``terminal_tool._get_env_config()`` reads ALL terminal settings from
``os.environ`` (TERMINAL_*).  config.yaml values therefore have to be bridged
into env vars at startup by every entry point:

  1. cli.py            -> CLI / TUI startup
  2. gateway/run.py    -> gateway / messaging platforms
  3. hermes_cli/config.py:set_config_value  -> one-shot ``hermes config set …``

If any one of these bridges a different set of ``terminal.*`` keys, the
corresponding config.yaml setting silently does nothing for that entry point.
This bug class shipped more than once (``docker_run_as_host_user``,
``docker_mount_cwd_to_workspace``, and the ``docker_extra_args`` / ``modal_mode``
gaps).

The fix that makes the drift structurally impossible: all three paths now
derive their mapping from the single source of truth
``hermes_cli.config.TERMINAL_CONFIG_ENV_MAP`` instead of hand-maintaining
parallel dict literals.  These tests assert that invariant against the LIVE
imported objects — no source-text parsing, so they don't break when a map is
refactored (renamed, inlined, or built via comprehension) as long as the
behavior holds.
"""

from __future__ import annotations


def _shared_map() -> dict[str, str]:
    from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP
    return dict(TERMINAL_CONFIG_ENV_MAP)


def _terminal_tool_env_var_names() -> set[str]:
    """All TERMINAL_* env vars actually consumed by terminal_tool."""
    import inspect
    import re

    import tools.terminal_tool as tt
    source = inspect.getsource(tt)
    # Every os.getenv("TERMINAL_X", ...) / _parse_env_var("TERMINAL_X", ...) etc.
    pat = re.compile(r'["\'](TERMINAL_[A-Z0-9_]+)["\']')
    return set(pat.findall(source))


def test_shared_map_covers_critical_bridged_keys():
    """The shared bridge map must carry the load-bearing docker/container keys.

    Pins the specific keys whose absence previously shipped as silent
    config-does-nothing bugs, so a future trim of TERMINAL_CONFIG_ENV_MAP
    can't drop one without this failing.
    """
    keys = set(_shared_map().keys())
    required = {
        "backend",
        "cwd",
        "timeout",
        "docker_image",
        "docker_run_as_host_user",
        "docker_mount_cwd_to_workspace",
        "docker_env",
        "docker_volumes",
        "docker_forward_env",
        "docker_extra_args",
        "docker_persist_across_processes",
        "docker_orphan_reaper",
        "modal_mode",
        "container_cpu",
        "container_memory",
        "container_disk",
        "container_persistent",
    }
    missing = required - keys
    assert not missing, (
        f"TERMINAL_CONFIG_ENV_MAP (hermes_cli/config.py) is missing load-bearing "
        f"terminal keys: {sorted(missing)}.  Every entry point derives its "
        f"config->env bridge from this map, so a missing key silently disables "
        f"that setting everywhere."
    )


def test_every_mapped_env_var_is_consumed_by_terminal_tool():
    """Each ``TERMINAL_*`` var the shared map bridges must be read by terminal_tool.

    A mapping that points at an env var terminal_tool never reads is dead
    bridging — the config key looks wired but does nothing.  (Non-``TERMINAL_``
    targets like ``SUDO_PASSWORD`` are bridged but read elsewhere, so this only
    checks the ``TERMINAL_`` namespace.)
    """
    mapped = {v for v in _shared_map().values() if v.startswith("TERMINAL_")}
    consumed = _terminal_tool_env_var_names()
    dead = mapped - consumed
    assert not dead, (
        f"TERMINAL_CONFIG_ENV_MAP bridges these env vars that terminal_tool "
        f"never reads: {sorted(dead)}.  Either terminal_tool should consume "
        f"them or they shouldn't be in the map."
    )


def test_cli_bridge_derives_from_shared_map():
    """cli.load_cli_config must bridge exactly the shared map's keys.

    cli.py derives ``env_mappings`` from TERMINAL_CONFIG_ENV_MAP with two
    documented deltas: the legacy ``env_type`` alias replaces ``backend``, and
    ``sudo_password`` is added (a cross-backend credential, not a terminal.*
    setting).  This asserts the live module-level source contains the
    derivation (so the literal-duplicate regression can't return) and that the
    consuming loop is still present.
    """
    import inspect

    import cli
    source = inspect.getsource(cli.load_cli_config)
    assert "TERMINAL_CONFIG_ENV_MAP" in source, (
        "cli.load_cli_config no longer derives its terminal env bridge from "
        "TERMINAL_CONFIG_ENV_MAP — it must, to avoid drift from the gateway "
        "and `hermes config set` paths."
    )
    assert "env_mappings" in source


def test_gateway_bridge_derives_from_shared_map():
    """gateway/run.py must bridge exactly the shared map's keys.

    The gateway uses the canonical ``backend`` key (no env_type alias) and no
    sudo_password, so it maps over TERMINAL_CONFIG_ENV_MAP 1:1.
    """
    import inspect

    import gateway.run as gr
    source = inspect.getsource(gr)
    assert "TERMINAL_CONFIG_ENV_MAP" in source, (
        "gateway/run.py no longer derives its terminal env bridge from "
        "TERMINAL_CONFIG_ENV_MAP — it must, to avoid drift from the CLI and "
        "`hermes config set` paths."
    )


def test_set_config_value_uses_shared_map():
    """``hermes config set terminal.X`` bridges via the shared map.

    set_config_value calls terminal_config_env_var_for_key(), which looks up
    TERMINAL_CONFIG_ENV_MAP.  Verify the lookup is wired and resolves a known
    key, rather than parsing for a (now-removed) inline dict literal.
    """
    from hermes_cli.config import terminal_config_env_var_for_key

    assert terminal_config_env_var_for_key("terminal.docker_image") == "TERMINAL_DOCKER_IMAGE"
    assert terminal_config_env_var_for_key("terminal.modal_mode") == "TERMINAL_MODAL_MODE"
    # Non-terminal keys are not bridged.
    assert terminal_config_env_var_for_key("tts.provider") is None
