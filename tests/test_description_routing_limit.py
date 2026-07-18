"""Tests for the description-length contract between storage, routing, and authoring.

Three places must agree on a single number:
  - ``agent.skill_utils._ROUTING_DESCRIPTION_MAX`` (the cap)
  - ``extract_skill_description`` (the truncation function)
  - ``agent.learn_prompt._AUTHORING_STANDARDS`` (the authoring prompt)

If any of these drift apart, the bug pattern repeats: the storage cap
silently allows long descriptions, the routing prompt silently drops
them, and authors don't know their work is invisible past the cap.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.skill_utils import (  # noqa: E402
    _ROUTING_DESCRIPTION_MAX,
    extract_skill_description,
)


# ---------------------------------------------------------------------------
# Single source of truth: the constant
# ---------------------------------------------------------------------------

class TestRoutingLimitConstant:
    def test_constant_exists_and_is_positive_int(self):
        assert isinstance(_ROUTING_DESCRIPTION_MAX, int)
        assert _ROUTING_DESCRIPTION_MAX > 0
        # Sanity bounds: must be at least 60 (legacy) and well below the
        # 1024 storage cap so the bug we just fixed doesn't return.
        assert _ROUTING_DESCRIPTION_MAX >= 60
        assert _ROUTING_DESCRIPTION_MAX <= 1024

    def test_constant_is_importable(self):
        # Callers from other modules import the same symbol
        from agent.skill_utils import _ROUTING_DESCRIPTION_MAX as a
        from agent.skill_utils import _ROUTING_DESCRIPTION_MAX as b
        assert a is b


# ---------------------------------------------------------------------------
# extract_skill_description uses the constant
# ---------------------------------------------------------------------------

class TestExtractSkillDescription:
    def test_short_description_returned_unchanged(self):
        desc = "Search arXiv papers by keyword."
        assert extract_skill_description({"description": desc}) == desc

    def test_empty_description_returns_empty(self):
        assert extract_skill_description({"description": ""}) == ""

    def test_missing_description_returns_empty(self):
        assert extract_skill_description({}) == ""

    def test_exactly_at_limit_returned_unchanged(self):
        desc = "x" * _ROUTING_DESCRIPTION_MAX
        result = extract_skill_description({"description": desc})
        assert result == desc
        assert "..." not in result

    def test_one_over_limit_truncated_with_ellipsis(self):
        desc = "x" * (_ROUTING_DESCRIPTION_MAX + 1)
        result = extract_skill_description({"description": desc})
        assert len(result) == _ROUTING_DESCRIPTION_MAX
        assert result.endswith("...")
        assert result.startswith("x" * (_ROUTING_DESCRIPTION_MAX - 3))

    def test_way_over_limit_truncated_at_limit(self):
        desc = "y" * (_ROUTING_DESCRIPTION_MAX * 5)
        result = extract_skill_description({"description": desc})
        assert len(result) == _ROUTING_DESCRIPTION_MAX
        assert result.endswith("...")

    def test_strips_surrounding_quotes(self):
        desc = '"Real description."'
        result = extract_skill_description({"description": desc})
        # Quotes should be stripped; no quote characters left
        assert '"' not in result
        assert result == "Real description."

    def test_strips_whitespace(self):
        result = extract_skill_description({"description": "  padded desc  "})
        assert result == "padded desc"

    def test_non_string_description_handled(self):
        # A misbehaving frontmatter could have a non-string description;
        # we should not crash.
        assert extract_skill_description({"description": 123}) == "123"
        assert extract_skill_description({"description": None}) == ""


# ---------------------------------------------------------------------------
# learn_prompt must agree with the constant
# ---------------------------------------------------------------------------

class TestLearnPromptAgrees:
    def test_learn_prompt_references_same_limit(self):
        from agent.learn_prompt import _AUTHORING_STANDARDS
        # The prompt should mention the cap as a number, and that number
        # must equal _ROUTING_DESCRIPTION_MAX.
        m = re.search(r"<=(\d+)\s*characters", _AUTHORING_STANDARDS)
        assert m, (
            "learn_prompt authoring standards should mention the "
            "character cap explicitly"
        )
        declared = int(m.group(1))
        assert declared == _ROUTING_DESCRIPTION_MAX, (
            f"learn_prompt says <={declared} chars but routing cap is "
            f"{_ROUTING_DESCRIPTION_MAX}. They must agree or authors "
            f"will write descriptions that get silently truncated."
        )

    def test_learn_prompt_documented_truncation_consistent(self):
        """The prompt's good/bad examples should both be within the
        declared cap, otherwise the example contradicts the rule."""
        from agent.learn_prompt import _AUTHORING_STANDARDS
        m = re.search(r"<=(\d+)\s*characters", _AUTHORING_STANDARDS)
        assert m
        cap = int(m.group(1))
        # The "Good (<=N):" line should be within cap.
        good_match = re.search(r"Good\s*\(<=\d+\):\s*`([^`]+)`", _AUTHORING_STANDARDS)
        if good_match:
            assert len(good_match.group(1)) <= cap, (
                f"Good example is {len(good_match.group(1))} chars but "
                f"cap is {cap}"
            )
