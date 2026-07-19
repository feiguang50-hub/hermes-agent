"""``hermes skill`` subcommand parser (singular).

Extracted from ``hermes_cli/main.py`` so the score subcommand lives next to
the other subcommand builders. Read-only surface — no skill mutations, no
side effects at import time. Handler injected to avoid importing main.
"""

from __future__ import annotations

from typing import Callable


def build_skill_parser(subparsers, *, cmd_skill: Callable) -> None:
    """Attach the ``skill`` subcommand to ``subparsers``."""
    skill_parser = subparsers.add_parser(
        "skill",
        help="Per-skill introspection (read-only). Currently: score.",
        description=(
            "Read-only views into a single skill's state. Does not modify "
            "skills, the curator, or any sidecar files. "
            "For installing / managing skill packages from registries, see "
            "``hermes skills`` (plural)."
        ),
    )
    skill_subparsers = skill_parser.add_subparsers(dest="skill_action")

    p_score = skill_subparsers.add_parser(
        "score",
        help="Show the 0–1 quality score for a skill (from compute_skill_score).",
        description=(
            "Calls agent.skill_scoring.compute_skill_score(name) and renders "
            "the score plus its components (success_rate, feedback_score, "
            "recency_decay, confidence, sample_size, last_outcome, "
            "last_rating). Read-only — does not record any new outcomes or "
            "feedback."
        ),
    )
    p_score.add_argument(
        "name",
        help="Skill name (must match exactly, case-sensitive).",
    )
    p_score.add_argument(
        "--json", action="store_true",
        help="Emit the raw compute_skill_score dict instead of the table.",
    )

    skill_parser.set_defaults(func=cmd_skill)