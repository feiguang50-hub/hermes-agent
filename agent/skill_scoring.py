"""Skill quality scoring — Task A of the self-improving evaluation redesign.

The curator today can only count *how often* a skill is loaded. That tells
you nothing about whether it actually helped — a buggy skill loaded 50
times is still buggy.

This module turns the outcome + user_feedback fields added to
``tools.skill_usage.py`` into a single 0-1 quality score per skill. The
score is a weighted blend of:

  * **success rate** — fraction of outcomes that were ``success`` (out of
    ``success + failure``). Corrected / abandoned count neither for nor
    against; ``unknown`` is excluded.
  * **user feedback** — ``(thumbs_up - thumbs_down) / (thumbs_up + thumbs_down)``
    mapped from [-1, 1] to [0, 1]. Skills with zero feedback score 0.5
    (neutral) rather than being penalized.
  * **recency decay** — ``exp(-days_since_use / 30)``. A skill that hasn't
    been touched in months decays toward 0 even if it has good historical
    scores; the curator should reconsider it.
  * **confidence** — ``min(1, total_outcomes / 5)``. Below 5 outcomes the
    score leans on recency decay because success_rate is too noisy.

The blend:

    if confidence < 1:
        score = confidence * blend_success_feedback + (1 - confidence) * recency
    else:
        score = blend_success_feedback

    blend_success_feedback = 0.6 * success_rate + 0.4 * feedback_score

This file is read-only with respect to the sidecar — all writes go through
``tools.skill_usage.record_outcome`` / ``record_user_feedback``.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from tools.skill_usage import (
    FEEDBACK_DOWN,
    FEEDBACK_UP,
    OUTCOME_FAILURE,
    OUTCOME_SUCCESS,
    get_record,
)

logger = logging.getLogger(__name__)

# Outcome blend weights (sum to 1.0). Tweak here without changing callers.
_WEIGHT_SUCCESS_RATE = 0.6
_WEIGHT_USER_FEEDBACK = 0.4

# Half-life for recency decay (days). A skill untouched for ``half_life``
# days scores 0.5 on the recency component; for 2*half_life it scores 0.25.
RECENCY_HALF_LIFE_DAYS = 30.0

# Minimum outcomes before we trust success_rate as a signal. Below this
# the score is damped toward recency.
CONFIDENCE_THRESHOLD = 5

# Neutral score used when no user feedback is available (avoid penalizing
# skills the user simply hasn't rated).
NEUTRAL_FEEDBACK_SCORE = 0.5


def _safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    if denominator <= 0:
        return default
    return numerator / denominator


def _parse_iso_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _success_rate(record: Dict[str, Any]) -> float:
    """Success fraction over success+failure outcomes.

    Corrected / abandoned / unknown do NOT count toward the denominator —
    they're "neutral" signals. Returns 0.5 when there are no resolved
    outcomes (the function-call "I don't know" answer).
    """
    outcomes = record.get("outcomes") or {}
    success = int(outcomes.get(OUTCOME_SUCCESS) or 0)
    failure = int(outcomes.get(OUTCOME_FAILURE) or 0)
    total_resolved = success + failure
    if total_resolved == 0:
        return 0.5
    return success / total_resolved


def _feedback_score(record: Dict[str, Any]) -> float:
    """Thumbs-up minus thumbs-down, mapped from [-1, 1] to [0, 1].

    0 thumbs = neutral (0.5). All up = 1.0. All down = 0.0.
    """
    fb = record.get("user_feedback") or {}
    up = int(fb.get(FEEDBACK_UP) or 0)
    down = int(fb.get(FEEDBACK_DOWN) or 0)
    if up == 0 and down == 0:
        return NEUTRAL_FEEDBACK_SCORE
    net = (up - down) / (up + down)  # in [-1, 1]
    return (net + 1.0) / 2.0


def _recency_decay(record: Dict[str, Any]) -> float:
    """Exponential decay from the most recent activity.

    Uses max(last_used_at, last_viewed_at, last_patched_at). Skills with no
    activity at all decay to 0 immediately — they have no signal of being
    useful, so the curator should consider archiving them.
    """
    latest_iso = None
    for key in ("last_used_at", "last_viewed_at", "last_patched_at"):
        raw = record.get(key)
        if not raw:
            continue
        if latest_iso is None or str(raw) > latest_iso:
            latest_iso = str(raw)
    if not latest_iso:
        return 0.0
    dt = _parse_iso_timestamp(latest_iso)
    if dt is None:
        return 0.0
    days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    if days < 0:
        days = 0.0
    return math.pow(0.5, days / RECENCY_HALF_LIFE_DAYS)


def _confidence(record: Dict[str, Any]) -> float:
    """How much we trust the success_rate signal (0-1)."""
    outcomes = record.get("outcomes") or {}
    total = sum(int(outcomes.get(k) or 0) for k in (OUTCOME_SUCCESS, OUTCOME_FAILURE))
    return min(1.0, total / CONFIDENCE_THRESHOLD)


def _blend_success_feedback(success_rate: float, feedback: float) -> float:
    return _WEIGHT_SUCCESS_RATE * success_rate + _WEIGHT_USER_FEEDBACK * feedback


def compute_skill_score(skill_name: str) -> Dict[str, Any]:
    """Compute a 0-1 quality score for *skill_name*.

    Returns a dict with the score and its components so callers can explain
    the result ("why is this skill scored 0.7?"). The score is in [0, 1];
    the components are also each in [0, 1]. If the skill has no record,
    returns a score of 0 with explanation.
    """
    record = get_record(skill_name)

    success_rate = _success_rate(record)
    feedback = _feedback_score(record)
    recency = _recency_decay(record)
    confidence = _confidence(record)
    blend = _blend_success_feedback(success_rate, feedback)

    if confidence < 1.0:
        score = confidence * blend + (1.0 - confidence) * recency
    else:
        score = blend

    # Clamp into [0, 1] defensively (float math can drift).
    score = max(0.0, min(1.0, score))

    outcomes = record.get("outcomes") or {}
    fb = record.get("user_feedback") or {}
    sample_size = sum(int(outcomes.get(k) or 0) for k in (OUTCOME_SUCCESS, OUTCOME_FAILURE))
    feedback_total = int(fb.get(FEEDBACK_UP) or 0) + int(fb.get(FEEDBACK_DOWN) or 0)

    return {
        "skill": skill_name,
        "score": round(score, 4),
        "components": {
            "success_rate": round(success_rate, 4),
            "feedback_score": round(feedback, 4),
            "recency_decay": round(recency, 4),
            "blend_success_feedback": round(blend, 4),
            "confidence": round(confidence, 4),
        },
        "weights": {
            "success_rate": _WEIGHT_SUCCESS_RATE,
            "user_feedback": _WEIGHT_USER_FEEDBACK,
        },
        "sample_size": sample_size,
        "feedback_total": feedback_total,
        "recency_half_life_days": RECENCY_HALF_LIFE_DAYS,
        "confidence_threshold": CONFIDENCE_THRESHOLD,
        "last_outcome": outcomes.get("last_outcome"),
        "last_outcome_at": outcomes.get("last_outcome_at"),
        "last_rating": fb.get("last_rating"),
    }


def score_many(skill_names) -> Dict[str, Dict[str, Any]]:
    """Compute scores for many skills in one call. Used by ``usage_report``
    style dashboards."""
    return {name: compute_skill_score(name) for name in skill_names}
