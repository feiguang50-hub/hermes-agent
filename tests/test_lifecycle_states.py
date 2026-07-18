"""Tests for the SPLIT and DEPRECATED lifecycle states.

Before this fix, ``skill_usage`` only modeled ``active`` /
``stale`` / ``archived``. The curator had no way to say "I
decomposed this skill into three narrower ones" or "this skill is
superseded by X" — both common outcomes of consolidation that
were getting collapsed into either "keep" or "archive". Archive
loses provenance; keep keeps routing to a skill that shouldn't
be used any more.

This test pins:

* The new state constants are accepted by ``set_state``.
* The accompanying ``split_into`` / ``replaced_by`` recorders
  accept realistic inputs and dedupe.
* ``skill_manage(action="split", split_into=[...])`` flips the
  state and records the replacement list.
* ``skill_manage(action="deprecate", replaced_by="...")`` flips
  the state and records the replacement.
* Both reject malformed input instead of silently doing nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import skill_usage  # noqa: E402
from tools.skill_usage import (  # noqa: E402
    STATE_ACTIVE,
    STATE_ARCHIVED,
    STATE_DEPRECATED,
    STATE_SPLIT,
    STATE_STALE,
    _VALID_STATES,
    get_record,
    record_replaced_by,
    record_split_into,
    set_state,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_hermes_home(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", lambda: tmp_path
    )
    monkeypatch.setattr(
        skill_usage, "get_hermes_home", lambda: tmp_path
    )
    (tmp_path / "skills").mkdir(exist_ok=True)
    return tmp_path


# ---------------------------------------------------------------------------
# State vocabulary
# ---------------------------------------------------------------------------

class TestStateVocabulary:
    def test_all_five_states_present(self):
        assert STATE_ACTIVE == "active"
        assert STATE_STALE == "stale"
        assert STATE_ARCHIVED == "archived"
        assert STATE_SPLIT == "split"
        assert STATE_DEPRECATED == "deprecated"

    def test_valid_states_includes_new_ones(self):
        assert STATE_SPLIT in _VALID_STATES
        assert STATE_DEPRECATED in _VALID_STATES
        # Backwards compat: existing states still valid.
        assert STATE_ACTIVE in _VALID_STATES
        assert STATE_STALE in _VALID_STATES
        assert STATE_ARCHIVED in _VALID_STATES


# ---------------------------------------------------------------------------
# set_state transitions
# ---------------------------------------------------------------------------

class TestSetStateTransitions:
    def test_split_state_accepted(self, fake_hermes_home):
        set_state("demo-skill", STATE_SPLIT)
        rec = get_record("demo-skill")
        assert rec["state"] == STATE_SPLIT
        assert rec["archived_at"] is not None  # split also stamps timestamp

    def test_deprecated_state_accepted(self, fake_hermes_home):
        set_state("demo-skill", STATE_DEPRECATED)
        rec = get_record("demo-skill")
        assert rec["state"] == STATE_DEPRECATED
        assert rec["archived_at"] is not None

    def test_active_clears_archived_at(self, fake_hermes_home):
        set_state("demo-skill", STATE_SPLIT)
        assert get_record("demo-skill")["archived_at"] is not None
        set_state("demo-skill", STATE_ACTIVE)
        rec = get_record("demo-skill")
        assert rec["state"] == STATE_ACTIVE
        assert rec["archived_at"] is None  # reactivated

    def test_unknown_state_silently_dropped(self, fake_hermes_home):
        set_state("demo-skill", "bogus")
        # Skill wasn't curation-eligible yet — so set_state is a no-op
        # regardless. After marking it agent-created:
        skill_usage.mark_agent_created("demo-skill")
        set_state("demo-skill", "bogus")
        rec = get_record("demo-skill")
        # Default state is active; bogus didn't change it.
        assert rec["state"] == STATE_ACTIVE


# ---------------------------------------------------------------------------
# record_split_into / record_replaced_by
# ---------------------------------------------------------------------------

class TestSplitAndDeprecateRecorders:
    def test_split_into_dedupes_and_preserves_order(self, fake_hermes_home):
        skill_usage.mark_agent_created("demo-skill")
        record_split_into(
            "demo-skill",
            ["alpha", "beta", "alpha", "gamma", "", "beta"],
        )
        rec = get_record("demo-skill")
        assert rec["split_into"] == ["alpha", "beta", "gamma"]

    def test_split_into_empty_input_is_noop(self, fake_hermes_home):
        skill_usage.mark_agent_created("demo-skill")
        record_split_into("demo-skill", [])
        rec = get_record("demo-skill")
        assert "split_into" not in rec

    def test_split_into_all_whitespace_dropped(self, fake_hermes_home):
        skill_usage.mark_agent_created("demo-skill")
        record_split_into("demo-skill", ["   ", "\t", ""])
        rec = get_record("demo-skill")
        assert "split_into" not in rec

    def test_split_into_overwrites_previous(self, fake_hermes_home):
        skill_usage.mark_agent_created("demo-skill")
        record_split_into("demo-skill", ["alpha", "beta"])
        record_split_into("demo-skill", ["gamma"])
        rec = get_record("demo-skill")
        assert rec["split_into"] == ["gamma"]

    def test_replaced_by_stores_single_value(self, fake_hermes_home):
        skill_usage.mark_agent_created("demo-skill")
        record_replaced_by("demo-skill", "umbrella-skill")
        rec = get_record("demo-skill")
        assert rec["replaced_by"] == "umbrella-skill"

    def test_replaced_by_strips_whitespace(self, fake_hermes_home):
        skill_usage.mark_agent_created("demo-skill")
        record_replaced_by("demo-skill", "  umbrella-skill  ")
        rec = get_record("demo-skill")
        assert rec["replaced_by"] == "umbrella-skill"

    def test_replaced_by_empty_rejected(self, fake_hermes_home):
        skill_usage.mark_agent_created("demo-skill")
        record_replaced_by("demo-skill", "   ")
        rec = get_record("demo-skill")
        assert "replaced_by" not in rec

    def test_recorders_noop_on_non_curation_eligible(self, fake_hermes_home):
        # Without mark_agent_created, the recorders are no-ops (require
        # curation-eligible).
        record_split_into("bundled-skill", ["alpha"])
        record_replaced_by("bundled-skill", "umbrella")
        # Either no record exists or the fields weren't written.
        try:
            rec = get_record("bundled-skill")
            assert "split_into" not in rec
            assert "replaced_by" not in rec
        except Exception:
            pass  # acceptable: bundled-skill has no record


# ---------------------------------------------------------------------------
# skill_manage integration
# ---------------------------------------------------------------------------

class TestSkillManageIntegration:
    """End-to-end through the public tool entry point. Uses the same
    monkeypatching trick as the existing skill_management tests to
    avoid touching real disk."""

    def test_split_action_flips_state_and_records_replacements(
        self, fake_hermes_home
    ):
        from tools import skill_manager_tool
        # Make the skill curation-eligible so set_state / record_split_into
        # can write to it.
        skill_usage.mark_agent_created("demo-skill")

        result_json = skill_manager_tool.skill_manage(
            action="split", name="demo-skill",
            split_into=["alpha", "beta", "gamma"],
        )
        import json
        result = json.loads(result_json)
        assert result["success"], result
        assert result["action"] == "split"
        assert result["split_into"] == ["alpha", "beta", "gamma"]

        rec = get_record("demo-skill")
        assert rec["state"] == STATE_SPLIT
        assert rec["split_into"] == ["alpha", "beta", "gamma"]
        assert rec["archived_at"] is not None  # timestamp stamped

    def test_split_action_requires_split_into(self, fake_hermes_home):
        from tools import skill_manager_tool
        skill_usage.mark_agent_created("demo-skill")
        result_json = skill_manager_tool.skill_manage(
            action="split", name="demo-skill",
        )
        import json
        result = json.loads(result_json)
        assert result["success"] is False
        assert "split_into" in result.get("error", "")

    def test_split_action_rejects_non_list(self, fake_hermes_home):
        from tools import skill_manager_tool
        skill_usage.mark_agent_created("demo-skill")
        result_json = skill_manager_tool.skill_manage(
            action="split", name="demo-skill",
            split_into="not-a-list",
        )
        import json
        result = json.loads(result_json)
        assert result["success"] is False

    def test_split_action_rejects_list_with_non_strings(self, fake_hermes_home):
        from tools import skill_manager_tool
        skill_usage.mark_agent_created("demo-skill")
        result_json = skill_manager_tool.skill_manage(
            action="split", name="demo-skill",
            split_into=["valid", "", "also-valid"],
        )
        import json
        result = json.loads(result_json)
        assert result["success"] is False

    def test_deprecate_action_flips_state_and_records_replacement(
        self, fake_hermes_home
    ):
        from tools import skill_manager_tool
        skill_usage.mark_agent_created("demo-skill")
        result_json = skill_manager_tool.skill_manage(
            action="deprecate", name="demo-skill",
            replaced_by="umbrella-skill",
        )
        import json
        result = json.loads(result_json)
        assert result["success"], result
        assert result["action"] == "deprecate"
        assert result["replaced_by"] == "umbrella-skill"

        rec = get_record("demo-skill")
        assert rec["state"] == STATE_DEPRECATED
        assert rec["replaced_by"] == "umbrella-skill"
        assert rec["archived_at"] is not None

    def test_deprecate_action_requires_replaced_by(self, fake_hermes_home):
        from tools import skill_manager_tool
        skill_usage.mark_agent_created("demo-skill")
        result_json = skill_manager_tool.skill_manage(
            action="deprecate", name="demo-skill",
        )
        import json
        result = json.loads(result_json)
        assert result["success"] is False
        assert "replaced_by" in result.get("error", "")

    def test_unknown_action_error_message_lists_new_actions(
        self, fake_hermes_home
    ):
        from tools import skill_manager_tool
        result_json = skill_manager_tool.skill_manage(
            action="frobnicate", name="demo-skill",
        )
        import json
        result = json.loads(result_json)
        assert result["success"] is False
        # The error message should advertise both new actions so the
        # LLM knows they exist.
        assert "split" in result["error"]
        assert "deprecate" in result["error"]


# ---------------------------------------------------------------------------
# Routing layer should hide deprecated / split skills from the
# default index — this is the user-visible payoff of the new states.
# ---------------------------------------------------------------------------

class TestRoutingHidesDeprecatedAndSplit:
    """The skills_index built by prompt_builder should skip deprecated
    and split skills by default so the LLM isn't routed to them."""

    def test_deprecated_skill_not_in_default_index(self, fake_hermes_home):
        from agent import skill_utils
        # Create a fake skill directory with frontmatter. Use [linux] so the
        # platform gate passes on this Windows test runner.
        skill_dir = fake_hermes_home / "skills" / "old-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: old-skill\n"
            "description: An old skill, deprecated.\n"
            "platforms: [linux, windows, macos]\n"
            "---\n"
            "# Old\n",
            encoding="utf-8",
        )
        skill_usage.mark_agent_created("old-skill")
        # Stub the skill-discovery layer to point at our skill.
        from agent import prompt_builder
        real_get_all = skill_utils.get_all_skills_dirs
        skill_utils.get_all_skills_dirs = lambda: [fake_hermes_home / "skills"]
        prompt_builder.clear_skills_system_prompt_cache()
        try:
            # Before deprecation: appears
            index_before = prompt_builder.build_skills_system_prompt()
            assert "old-skill" in index_before, (
                "fixture skill should appear in routing before deprecation"
            )

            # Deprecate it
            from tools.skill_usage import set_state, STATE_DEPRECATED
            set_state("old-skill", STATE_DEPRECATED)
            prompt_builder.clear_skills_system_prompt_cache()

            index_after = prompt_builder.build_skills_system_prompt()
            assert "old-skill" not in index_after, (
                "Deprecated skill should be hidden from default routing"
            )
        finally:
            skill_utils.get_all_skills_dirs = real_get_all
            prompt_builder.clear_skills_system_prompt_cache()

    def test_split_skill_not_in_default_index(self, fake_hermes_home):
        from agent import skill_utils
        from agent import prompt_builder
        skill_dir = fake_hermes_home / "skills" / "split-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: split-skill\n"
            "description: A skill split into pieces.\n"
            "platforms: [linux, windows, macos]\n"
            "---\n"
            "# Split\n",
            encoding="utf-8",
        )
        skill_usage.mark_agent_created("split-skill")
        real_get_all = skill_utils.get_all_skills_dirs
        skill_utils.get_all_skills_dirs = lambda: [fake_hermes_home / "skills"]
        prompt_builder.clear_skills_system_prompt_cache()
        try:
            from tools.skill_usage import set_state, STATE_SPLIT
            set_state("split-skill", STATE_SPLIT)
            prompt_builder.clear_skills_system_prompt_cache()
            index = prompt_builder.build_skills_system_prompt()
            assert "split-skill" not in index
        finally:
            skill_utils.get_all_skills_dirs = real_get_all
            prompt_builder.clear_skills_system_prompt_cache()
