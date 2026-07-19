"""Curator-specific pre/post_tool_call hooks.

Adds three layers of enforcement around the LLM-driven curator pass:

  (a) **Dry-run hard block** — when curator is invoked in dry-run mode,
      every mutating ``skill_manage`` call (``patch`` / ``create`` /
      ``write_file`` / ``delete``) is intercepted with ``{"action": "block"}``
      BEFORE the tool executes. This is independent of the LLM following the
      ``CURATOR_DRY_RUN_BANNER`` text in the prompt — if the LLM does
      call a mutating tool, this hook stops it.

  (b) **Keyword retention check** — when curator is in real-execution mode,
      a hook inspects the ``file_content`` / ``content`` / ``new_string``
      arguments and computes the fraction of the target skill's identity
      keywords (name + description keywords + section headings) that
      still appear in the new content. Below the configured threshold,
      the hook returns ``{"action": "approve", ...}`` so the call is
      escalated to the human-approval gate (``tools.approval.request_tool_approval``)
      — same gate Tier-2 dangerous shell patterns use.

  (c) **Post-tool audit** — every curator-time tool call (whether blocked,
      approved, or normal) is written to a JSON Lines audit log so the
      decision rules and their outcomes can be analysed post-hoc.

Thread-local state is used to scope the hooks to the curator LLM pass only;
non-curator agents are not affected. ``enter_curator_context()`` and
``exit_curator_context()`` are the entry / exit points. They are normally
called by ``agent.curator._run_llm_review``; tests can drive them directly.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Fraction of the target skill's keywords that must appear in the new
# content. Below this we escalate to the human-approval gate rather than
# silently allow or silently block. 0.5 = at least half must be preserved.
DEFAULT_RETENTION_THRESHOLD = 0.5

# Actions that mutate a skill's content or existence. Non-mutating actions
# (view, list) are not gated. This MUST stay in sync with the mutating
# actions in ``SKILL_MANAGE_SCHEMA`` (tools/skill_manager_tool.py) — any
# mutating action missing here silently bypasses BOTH the dry-run block and
# the keyword-retention check.
#
# `edit` replaces the whole SKILL.md body and `remove_file` deletes an
# auxiliary file; both mutate and must be gated like `patch` / `write_file`.
# Omitting them let a curator route around the guard with
# `skill_manage action="edit"` (full-body rewrite) or `action="remove_file"`.
#
# `split` and `deprecate` flip a skill's lifecycle state (hidden from
# routing, possibly with a pointer to a replacement) without touching the
# SKILL.md on disk. They are mutating for gating purposes — they are
# irreversible without an explicit state flip back, and a curator could
# otherwise route around the dry-run guard by recording a split without
# ever touching files.
_MUTATING_ACTIONS = frozenset({
    "patch", "create", "edit", "write_file", "remove_file",
    "delete", "split", "deprecate",
})

# Argument fields where the LLM puts the new skill text. These are the
# fields we scan for keyword retention.
_CONTENT_FIELDS = ("file_content", "content", "new_string")

# Argument names the LLM may use to declare which technical skills are
# being absorbed into an umbrella during a ``create`` call. When any of
# these is present, the keyword-retention check uses the union of the
# umbrella's keywords AND the absorbed skills' keywords — so the LLM
# satisfies the gate by mentioning at least one absorbed skill's name
# in the new content, rather than having to self-reference the
# umbrella itself.
_MERGED_SKILLS_KEYS = (
    "merged_skills",
    "absorbed_from",
    "source_skills",
    "cluster_members",
    "absorbed_skills",
)

# Cap on content size we pass to the audit log (full content may be huge
# for a SKILL.md patch — we only need a preview for forensics).
_AUDIT_ARG_TRUNCATE = 200
_AUDIT_RESULT_TRUNCATE = 300

# Skill keywords cap. Beyond this we cap the list so the retention check
# stays fast and the audit log doesn't drown in 200-keyword arrays.
_MAX_KEYWORDS = 30

# Body cap for frontmatter / heading extraction (read only this many
# chars of the SKILL.md body to keep keyword extraction cheap).
_BODY_LIMIT = 2000


# ---------------------------------------------------------------------------
# Lightweight stopword list for keyword extraction
# ---------------------------------------------------------------------------
# English + a small Chinese stopword set. Goal is "good enough" filtering
# for the kinds of words that appear in SKILL.md descriptions, not a
# linguistic-grade tokenizer. A custom plugin that needs better NLP can
# override ``extract_skill_keywords`` entirely.

_STOPWORDS_EN = frozenset({
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "should", "could", "may", "might", "must", "shall", "can",
    "this", "that", "these", "those", "i", "you", "he", "she", "it",
    "we", "they", "what", "which", "who", "whom", "whose", "where",
    "when", "why", "how", "all", "any", "both", "each", "few", "more",
    "most", "other", "some", "such", "no", "nor", "not", "only", "own",
    "same", "so", "than", "too", "very", "just", "into", "over", "after",
    "before", "above", "below", "between", "out", "off", "again", "further",
    "then", "once", "here", "there", "up", "down", "if", "because",
    "while", "about", "against", "around", "through", "during", "without",
    "within", "use", "uses", "used", "using", "via", "your", "their",
    "our", "its", "his", "her", "one", "two", "also",
})
_STOPWORDS_ZH = frozenset({
    "的", "了", "是", "在", "和", "与", "或", "也", "都", "就", "把",
    "被", "对", "为", "给", "从", "到", "以", "及", "可", "会", "能",
    "请", "这个", "那个", "它", "他", "她", "我们", "你们", "他们",
})
_ALL_STOPWORDS = _STOPWORDS_EN | _STOPWORDS_ZH

# ASCII word / identifier or single CJK char. CJK has no word boundaries,
# so we tokenize per character for Chinese; for English we keep
# dash/underscore as part of the token so skill names like
# ``pr-triage-salvage`` match as one token.
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,}|[\u4e00-\u9fff]")

# Detect whether a keyword has any CJK char (different matching strategy).
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    """Lowercased token list. ASCII words / identifiers + CJK chars."""
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def extract_skill_keywords(
    skill_name: str,
    skill_path: Optional[Path] = None,
    *,
    body_limit: int = _BODY_LIMIT,
) -> List[str]:
    """Build the keyword list we expect to be preserved when *skill_name*
    is patched or merged into an umbrella.

    Sources, in priority order:

    1. The skill name itself (with ``-`` ↔ ``_`` variants). Always present
       so the retention check is a meaningful identity check.
    2. The ``description`` field of the SKILL.md frontmatter, tokenized
       and stopword-filtered. Capped at 20 tokens.
    3. Level-2 section headings from the SKILL.md body (e.g. ``## Usage``,
       ``## Configuration``). Capped at 5 tokens per heading.
    4. If the skill file isn't readable, only the name is returned — the
       retention check still has something to compare against.

    Returns a deduplicated, order-preserving list, name variants first.
    """
    keywords: List[str] = []

    # 1. Name variants
    if skill_name:
        keywords.append(skill_name)
        if "-" in skill_name:
            keywords.append(skill_name.replace("-", "_"))
        if "_" in skill_name:
            keywords.append(skill_name.replace("_", "-"))

    # 2 + 3. SKILL.md frontmatter + headings
    if skill_path is not None:
        try:
            from agent.skill_utils import parse_frontmatter
            md = Path(skill_path) / "SKILL.md"
            if md.exists():
                content = md.read_text(encoding="utf-8")
                fm, body = parse_frontmatter(content)

                desc = (fm.get("description") or "").strip()
                if desc:
                    desc_tokens = _tokenize(desc)
                    desc_kw = [
                        t for t in desc_tokens
                        if t not in _ALL_STOPWORDS
                        and (len(t) >= 3 or any("\u4e00" <= c <= "\u9fff" for c in t))
                    ]
                    keywords.extend(desc_kw[:20])

                # Section headings (## only) — body capped to keep this cheap
                truncated = body[:body_limit] if body else ""
                for m in re.finditer(r"^##\s+(.+?)$", truncated, re.MULTILINE):
                    heading = m.group(1).strip()
                    heading_kw = [
                        t for t in _tokenize(heading)
                        if t not in _ALL_STOPWORDS and len(t) >= 3
                    ]
                    keywords.extend(heading_kw[:5])
        except Exception as e:
            logger.debug("extract_skill_keywords: failed to read %s: %s", skill_path, e)

    # Deduplicate while preserving order
    seen = set()
    out: List[str] = []
    for k in keywords:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out[:_MAX_KEYWORDS]


def compute_keyword_retention(
    new_content: str,
    keywords: List[str],
) -> Tuple[List[str], List[str], float]:
    """Compute keyword retention of *new_content* against *keywords*.

    Returns ``(preserved, missing, ratio)``:

    - **preserved**: keywords found in *new_content* (whole-word for ASCII,
      substring for CJK).
    - **missing**: keywords absent.
    - **ratio**: ``len(preserved) / len(keywords)`` (1.0 when keywords is empty).

    The threshold to escalate to the human-approval gate is configurable;
    see ``DEFAULT_RETENTION_THRESHOLD``.
    """
    if not keywords:
        return [], [], 1.0
    if not new_content:
        return [], list(keywords), 0.0

    preserved: List[str] = []
    missing: List[str] = []
    for kw in keywords:
        if _CJK_RE.search(kw):
            # CJK keywords use substring match (no word boundaries in CJK).
            # The CJK-specific fix (char n-grams) is intentionally deferred.
            matched = kw in new_content
        else:
            # ASCII keywords: tolerate `-`, `_`, and whitespace as
            # OPTIONAL separators between sub-tokens, so a name like
            # `github-code-review` matches content that writes
            # "GitHub Code Review" or `github_code_review`. Word
            # boundaries (`\b`) are kept at the outer edges so we don't
            # match `github-codes-review` (the trailing `s` is a real
            # word boundary, not a separator).
            parts = [p for p in re.split(r'[-_\s]+', kw) if p]
            escaped = [re.escape(p) for p in parts]
            pattern = r'\b' + r'[\s\-_]?'.join(escaped) + r'\b'
            matched = bool(re.search(pattern, new_content, re.IGNORECASE))
        (preserved if matched else missing).append(kw)
    ratio = len(preserved) / len(keywords)
    return preserved, missing, ratio


# ---------------------------------------------------------------------------
# Thread-local curator context
# ---------------------------------------------------------------------------
# Hooks are global (they live on the PluginManager singleton), so we use
# thread-local state to scope them to the curator LLM pass. The curator
# calls ``enter_curator_context`` immediately before spawning / invoking
# the LLM agent, and ``exit_curator_context`` immediately after.

_curator_state = threading.local()


def enter_curator_context(
    *,
    dry_run: bool,
    audit_log_path: Optional[Path] = None,
) -> None:
    """Mark the current thread as inside a curator LLM pass.

    Idempotent — calling twice replaces the prior state. Must be paired
    with ``exit_curator_context`` in a ``try/finally`` so the audit log
    handle is closed even if the LLM call raises.
    """
    # Close any prior handle (defensive)
    if getattr(_curator_state, "audit_handle", None) is not None:
        try:
            _curator_state.audit_handle.close()
        except Exception:
            pass

    _curator_state.dry_run = bool(dry_run)
    _curator_state.audit_log_path = audit_log_path
    _curator_state.skills_seen = {}      # name -> List[str] (lazy-loaded)
    _curator_state.audit_handle = None

    if audit_log_path is not None:
        try:
            audit_log_path.parent.mkdir(parents=True, exist_ok=True)
            # Line-buffered so we don't lose records on crash
            _curator_state.audit_handle = open(
                audit_log_path, "a", encoding="utf-8", buffering=1,
            )
        except Exception as e:
            logger.warning("curator_hooks: failed to open audit log: %s", e)
            _curator_state.audit_handle = None


def exit_curator_context() -> None:
    """Clear thread-local curator state and close the audit handle."""
    if getattr(_curator_state, "audit_handle", None) is not None:
        try:
            _curator_state.audit_handle.close()
        except Exception:
            pass
    _curator_state.dry_run = False
    _curator_state.audit_log_path = None
    _curator_state.skills_seen = {}
    _curator_state.audit_handle = None


def is_curator_context_active() -> bool:
    """True iff a curator LLM pass is currently scoped to this thread."""
    return bool(getattr(_curator_state, "dry_run", False)) \
        or getattr(_curator_state, "audit_handle", None) is not None


def _state() -> Optional[Dict[str, Any]]:
    """Return a snapshot of the thread-local state, or None if inactive."""
    if not is_curator_context_active():
        return None
    return {
        "dry_run": bool(getattr(_curator_state, "dry_run", False)),
        "audit_log_path": getattr(_curator_state, "audit_log_path", None),
        "skills_seen": getattr(_curator_state, "skills_seen", {}),
        "audit_handle": getattr(_curator_state, "audit_handle", None),
    }


# ---------------------------------------------------------------------------
# Audit log writer
# ---------------------------------------------------------------------------

def _write_audit(record: Dict[str, Any]) -> None:
    """Write one JSON Lines record to the audit log (if open)."""
    state = _state()
    if state is None:
        return
    handle = state.get("audit_handle")
    if handle is None:
        return
    try:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as e:
        logger.debug("curator_hooks: audit write failed: %s", e)


def _scrub_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Truncate long string values in args for compact audit logging."""
    out: Dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > _AUDIT_ARG_TRUNCATE:
            out[k] = v[:_AUDIT_ARG_TRUNCATE] + f"... (+{len(v) - _AUDIT_ARG_TRUNCATE} chars)"
        else:
            out[k] = v
    return out


def _preview_result(result: Any) -> str:
    """Render a tool result for the audit log (truncated)."""
    if result is None:
        return ""
    s = str(result)
    if len(s) > _AUDIT_RESULT_TRUNCATE:
        return s[:_AUDIT_RESULT_TRUNCATE] + f"... (+{len(s) - _AUDIT_RESULT_TRUNCATE} chars)"
    return s


# ---------------------------------------------------------------------------
# Pre-tool-call hook
# ---------------------------------------------------------------------------

def curator_pre_tool_call_hook(
    *,
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    middleware_trace: Optional[List[Dict[str, Any]]] = None,
    **_unused: Any,
) -> Optional[Dict[str, Any]]:
    """Curator-only pre-tool-call hook.

    Parameter names mirror the Hermes invoke_hook contract: ``tool_name``
    and ``args`` (not ``tool_name`` / ``args``). See
    ``hermes_cli/plugins.py::_get_pre_tool_call_directive_details`` and
    ``invoke_hook("pre_tool_call", tool_name=..., args=..., ...)``.

    Behavior:

    - No-op outside a curator LLM pass (so other agents are unaffected).
    - No-op for tools other than ``skill_manage``.
    - No-op for non-mutating ``skill_manage`` actions (view, list, etc.).

    In **dry-run mode**, any mutating skill_manage call returns
    ``{"action": "block", "message": ...}``. The LLM should not have
    called a mutating tool in dry-run, but this enforcement doesn't
    depend on the LLM following the prompt — it stops the call at the
    hook layer.

    In **real-execution mode**, the new content is checked against the
    target skill's identity keywords. If fewer than
    ``DEFAULT_RETENTION_THRESHOLD`` of the keywords survive, the call
    returns ``{"action": "approve", ...}`` so the existing
    ``tools.approval.request_tool_approval`` gate can prompt the human.
    The LLM cannot bypass this.

    All three outcomes (block / approve / allow) are written to the
    audit log with the rule evidence so the decision is reproducible.
    """
    state = _state()
    if state is None:
        return None
    # Diagnostic: log every pre_tool_call invocation when the curator
    # hook context is active, so we can see the hook fired even when
    # the tool isn't skill_manage or terminal. Cheap (one JSONL line per
    # call) and indispensable for verifying the hook is wired into the
    # real conversation loop. Set CURATOR_HOOK_DEBUG=0 to silence.
    _observed = os.environ.get("CURATOR_HOOK_DEBUG", "1") != "0"
    if _observed and tool_name not in ("skill_manage", "terminal"):
        _write_audit({
            "ts": datetime.now(timezone.utc).isoformat(),
            "hook": "pre_tool_call",
            "verdict": "observed",
            "tool": tool_name,
            "action": None,
            "name": None,
            "tool_call_id": tool_call_id,
            "session_id": session_id,
            "note": "curator hook context active but tool != skill_manage/terminal; skipping check",
        })
        return None
    # Bug #2 fix: terminal calls can mutate skill files via shell commands
    # (rm/mv/cp/redirect/sed -i etc.), bypassing the skill_manage guard.
    # Detect mutating shell commands that target skill paths and apply the
    # same dry-run / retention semantics as skill_manage.
    if tool_name == "terminal":
        return _check_terminal_skill_mutation(
            args=args, state=state,
            tool_call_id=tool_call_id, session_id=session_id,
        )
    if tool_name != "skill_manage":
        return None
    if not isinstance(args, dict):
        return None

    action = args.get("action")
    if action not in _MUTATING_ACTIONS:
        return None

    target = args.get("name")
    target_str = target if isinstance(target, str) and target else ""

    # ===== (a) Dry-run hard block =====
    if state["dry_run"]:
        message = (
            f"[curator-guard] DRY-RUN mode is active: skill_manage "
            f"action={action!r} name={target_str!r} is BLOCKED. "
            f"The LLM should not have called a mutating tool under "
            f"dry-run; this enforcement is independent of the LLM "
            f"following the prompt banner."
        )
        _write_audit({
            "ts": datetime.now(timezone.utc).isoformat(),
            "hook": "pre_tool_call",
            "verdict": "block_dry_run",
            "tool": tool_name,
            "action": action,
            "name": target_str,
            "args": _scrub_args(args),
            "tool_call_id": tool_call_id,
            "session_id": session_id,
            "message": message,
        })
        return {"action": "block", "message": message}

    # ===== (b) Keyword retention check (real execution) =====
    if not target_str:
        # Can't validate without a name; allow but log.
        _write_audit({
            "ts": datetime.now(timezone.utc).isoformat(),
            "hook": "pre_tool_call",
            "verdict": "allow_no_target",
            "tool": tool_name,
            "action": action,
            "name": target_str,
            "tool_call_id": tool_call_id,
            "session_id": session_id,
            "note": "skill_manage call had no 'name' arg; retention check skipped",
        })
        return None

    keywords = _get_keywords_for(
        state, target_str,
        extra_skills=_extract_merged_skills(args),
    )
    if not keywords:
        return None  # nothing to compare against

    # Concatenate every content field the LLM may have set
    haystack_parts: List[str] = []
    for key in _CONTENT_FIELDS:
        v = args.get(key)
        if isinstance(v, str):
            haystack_parts.append(v)
    haystack = "\n".join(haystack_parts)
    if not haystack:
        # delete action with no body, or create with only metadata — no text
        # to validate. Allow.
        _write_audit({
            "ts": datetime.now(timezone.utc).isoformat(),
            "hook": "pre_tool_call",
            "verdict": "allow_no_content",
            "tool": tool_name,
            "action": action,
            "name": target_str,
            "tool_call_id": tool_call_id,
            "session_id": session_id,
            "note": "no content field to validate",
        })
        return None

    preserved, missing, ratio = compute_keyword_retention(haystack, keywords)

    if ratio < DEFAULT_RETENTION_THRESHOLD:
        # Build a human-friendly reason that names the worst offenders
        name_variants = {target_str, target_str.replace("-", "_"), target_str.replace("_", "-")}
        name_missing = [m for m in missing if m in name_variants]
        kw_missing = [m for m in missing if m not in name_variants]

        reason_bits: List[str] = []
        if name_missing and len(name_variants) == len(name_missing):
            # All name variants missing — definitely a wrong-target patch
            reason_bits.append(f"target skill NAME {target_str!r} not found in patch content")
        elif name_missing:
            reason_bits.append(
                f"name variants missing: {name_missing} (some may be in content already)"
            )
        if kw_missing:
            preview = ", ".join(kw_missing[:8])
            if len(kw_missing) > 8:
                preview += f" (+{len(kw_missing) - 8} more)"
            reason_bits.append(
                f"keyword retention {ratio:.0%} < {DEFAULT_RETENTION_THRESHOLD:.0%} threshold; "
                f"missing keywords: [{preview}]"
            )

        message = (
            f"[curator-guard] skill_manage action={action!r} name={target_str!r} "
            f"requires human approval — " + "; ".join(reason_bits)
        )
        _write_audit({
            "ts": datetime.now(timezone.utc).isoformat(),
            "hook": "pre_tool_call",
            "verdict": "approve_needed",
            "tool": tool_name,
            "action": action,
            "name": target_str,
            "retention_ratio": round(ratio, 3),
            "preserved": preserved,
            "missing": missing,
            "args": _scrub_args(args),
            "tool_call_id": tool_call_id,
            "session_id": session_id,
            "message": message,
        })
        return {
            "action": "approve",
            "message": message,
            # Stable rule key: same target+action always goes to the same
            # allowlist slot, so [a]lways decisions persist cleanly.
            "rule_key": f"curator_guard:{target_str}:{action}",
        }

    # All checks passed — allow, but still log the decision
    _write_audit({
        "ts": datetime.now(timezone.utc).isoformat(),
        "hook": "pre_tool_call",
        "verdict": "allow",
        "tool": tool_name,
        "action": action,
        "name": target_str,
        "retention_ratio": round(ratio, 3),
        "preserved": preserved,
        "missing": missing,
        "args": _scrub_args(args),
        "tool_call_id": tool_call_id,
        "session_id": session_id,
    })
    return None


def _get_keywords_for(
    state: Dict[str, Any],
    skill_name: str,
    extra_skills: Optional[List[str]] = None,
) -> List[str]:
    """Lazy-load and cache keywords for *skill_name* on the thread state.

    When *extra_skills* is provided, the returned list is the union of
    the target's keywords and each extra skill's keywords (deduped,
    order-preserving, target first). This is how the umbrella-creation
    rule works: instead of demanding the new content mention the
    umbrella's own name, we accept mention of any of the absorbed
    technical skills' names.

    Caching key includes the extra_skills tuple so different umbrella
    compositions don't collide.
    """
    cache = state["skills_seen"]
    cache_key: Optional[Tuple[str, ...]] = None
    if extra_skills:
        cache_key = (skill_name, *sorted(set(extra_skills)))
    if cache_key is not None and cache_key in cache:
        return cache[cache_key]
    if cache_key is None and skill_name in cache:
        return cache[skill_name]

    keywords: List[str] = list(_load_skill_keywords(skill_name))

    for extra in extra_skills or []:
        if not isinstance(extra, str) or not extra or extra == skill_name:
            continue
        keywords.extend(_load_skill_keywords(extra))

    # Deduplicate while preserving order
    seen = set()
    deduped: List[str] = []
    for k in keywords:
        if k and k not in seen:
            seen.add(k)
            deduped.append(k)

    if cache_key is not None:
        cache[cache_key] = deduped
    else:
        cache[skill_name] = deduped
    return deduped


def _load_skill_keywords(skill_name: str) -> List[str]:
    """Read a skill's keywords from disk (or return name-only fallback)."""
    path = None
    try:
        from agent.skill_utils import get_all_skills_dirs
        for skills_dir in get_all_skills_dirs() or []:
            candidate = Path(skills_dir) / skill_name
            if candidate.is_dir():
                path = candidate
                break
        # Category-nested layout: <skills_dir>/<category>/<skill>/SKILL.md.
        # The flat lookup above only checks <skills_dir>/<name>, so it misses
        # every nested skill — which is the real-world norm. Fall back to the
        # frontmatter-name resolver skill_usage already uses (it rglobs
        # SKILL.md and matches the `name:` field, handling both layouts).
        # Without this, nested skills got a name-only keyword set and the
        # keyword-retention guard was effectively inert on real libraries.
        if path is None:
            try:
                from tools.skill_usage import _find_skill_dir
                resolved = _find_skill_dir(skill_name)
                if resolved is not None:
                    path = resolved
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("_load_skill_keywords: nested lookup failed: %s", e)
    except Exception as e:
        logger.debug("_load_skill_keywords: lookup failed: %s", e)
    return extract_skill_keywords(skill_name, path)


def _extract_merged_skills(args: Dict[str, Any]) -> List[str]:
    """Pull a list of absorbed/merged skill names from the tool call args.

    Accepts a single string or a list of strings under any of the keys
    listed in ``_MERGED_SKILLS_KEYS``. Returns deduplicated, order-
    preserving list (first occurrence wins).
    """
    seen = set()
    out: List[str] = []
    for key in _MERGED_SKILLS_KEYS:
        v = args.get(key)
        if isinstance(v, str):
            candidates = [v]
        elif isinstance(v, list):
            candidates = [x for x in v if isinstance(x, str)]
        elif isinstance(v, dict):
            # tolerate ``{"skills": [...]}`` shape
            inner = v.get("skills") or v.get("names")
            candidates = inner if isinstance(inner, list) else []
            candidates = [x for x in candidates if isinstance(x, str)]
        else:
            continue
        for c in candidates:
            c = c.strip()
            if c and c not in seen:
                seen.add(c)
                out.append(c)
    return out


# ---------------------------------------------------------------------------
# Post-tool-call hook
# ---------------------------------------------------------------------------

def curator_post_tool_call_hook(
    *,
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    duration_ms: int = 0,
    status: str = "ok",
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    middleware_trace: Optional[List[Dict[str, Any]]] = None,
    **_unused: Any,
) -> None:
    """Post-tool-call hook: write the actual execution result to the
    audit log. The pre-hook records the decision rule + verdict; this
    one records what actually happened (status / duration / error).

    Parameter names mirror the Hermes invoke_hook contract: ``tool_name``
    and ``args`` (not ``function_name`` / ``function_args``). See
    ``model_tools.py::_emit_post_tool_call_hook`` and
    ``hermes_cli/plugins.py:invoke_hook("post_tool_call", ...)``.
    """
    state = _state()
    if state is None:
        return
    if tool_name != "skill_manage":
        return

    tool_args = args or {}
    _write_audit({
        "ts": datetime.now(timezone.utc).isoformat(),
        "hook": "post_tool_call",
        "tool": tool_name,
        "action": tool_args.get("action"),
        "name": tool_args.get("name"),
        "tool_call_id": tool_call_id,
        "session_id": session_id,
        "duration_ms": duration_ms,
        "status": status,
        "error_type": error_type,
        "error_message": error_message,
        "result_preview": _preview_result(result),
    })


# ---------------------------------------------------------------------------
# Registration helpers
# ---------------------------------------------------------------------------
# These manage the lifetime of the global hook registration. They are
# idempotent and safe to call multiple times.

_HOOKS_REGISTERED = False
_PRE_HOOK_REF = curator_pre_tool_call_hook
_POST_HOOK_REF = curator_post_tool_call_hook


def register_curator_hooks() -> bool:
    """Register both hooks on the global PluginManager. Returns True if
    the call resulted in a state change (i.e. we weren't already registered).

    Note: Hermes' public ``LoadedPlugin.register_hook`` is the documented
    path, but the curator is a built-in module, not a discovered plugin.
    We register directly on the PluginManager's ``_hooks`` dict — the same
    underlying storage — so the hook is visible to the standard
    ``invoke_hook`` path the agent loop uses.
    """
    global _HOOKS_REGISTERED
    if _HOOKS_REGISTERED:
        return False
    try:
        from hermes_cli.plugins import get_plugin_manager
        mgr = get_plugin_manager()
        for hook_name, hook_ref in (
            ("pre_tool_call", _PRE_HOOK_REF),
            ("post_tool_call", _POST_HOOK_REF),
        ):
            mgr._hooks.setdefault(hook_name, []).append(hook_ref)
        _HOOKS_REGISTERED = True
        logger.debug("curator_hooks: registered pre/post_tool_call")
        return True
    except Exception as e:
        logger.warning("curator_hooks: registration failed: %s", e)
        return False


def unregister_curator_hooks() -> bool:
    """Remove the curator hooks from the global PluginManager. Returns
    True if the call resulted in a state change."""
    global _HOOKS_REGISTERED
    if not _HOOKS_REGISTERED:
        return False
    try:
        from hermes_cli.plugins import get_plugin_manager
        mgr = get_plugin_manager()
        for hook_name, hook_ref in (
            ("pre_tool_call", _PRE_HOOK_REF),
            ("post_tool_call", _POST_HOOK_REF),
        ):
            callbacks = mgr._hooks.get(hook_name, [])
            mgr._hooks[hook_name] = [
                cb for cb in callbacks if cb is not hook_ref
            ]
        _HOOKS_REGISTERED = False
        logger.debug("curator_hooks: unregistered")
        return True
    except Exception as e:
        logger.warning("curator_hooks: unregistration failed: %s", e)
        return False


def are_hooks_registered() -> bool:
    return _HOOKS_REGISTERED


# ---------------------------------------------------------------------------
# Bug #2 fix: terminal tool can mutate skill files via shell commands,
# bypassing the skill_manage dry-run and retention guards. This block
# detects mutating shell commands that target skill paths and applies
# the same dry-run / approval-gate semantics as skill_manage.
# ---------------------------------------------------------------------------

# Shell tokens / patterns that mutate the filesystem. Conservative: only
# match clear destruction / replacement, NOT mkdir/chmod/touch which are
# often legitimate setup.
_MUTATING_SHELL_PATTERNS: Tuple[str, ...] = (
    r"\brm\b(?!\s+--)",                       # rm, but not rm --help/--version
    r"\bmv\b",                                # mv = delete source + create dest
    r"\bcp\b",                                # cp into skill dir = new file
    r"\bsed\s+-i\b",                          # in-place edit
    r"\bperl\s+-i\b",                         # in-place edit (perl)
    r"\btee\b",                               # writes to file
    r"\bdd\b[^|;]*\bof=",                     # dd ... of=path (overwrites)
    r">>\s*\S|>\s*[~./\w]",                   # > or >> redirect to any non-space
    r"\btruncate\b",                          # explicit truncate
)


def _is_mutating_shell_command(command: str) -> bool:
    """Return True if *command* likely mutates files."""
    if not command:
        return False
    for pat in _MUTATING_SHELL_PATTERNS:
        if re.search(pat, command):
            return True
    return False


def _extract_paths_from_command(command: str) -> List[str]:
    """Extract path-like tokens from a shell command.

    Conservative: returns /-prefixed, ~/, ./, ../, absolute (POSIX or
    Windows), or any token containing '/' or '\\'. Bare names without
    '/' (e.g. ``baz`` in ``cp foo bar baz``) are NOT included — too
    ambiguous, would produce false positives like matching ``baz`` to a
    path called ``baz`` somewhere in skills.
    """
    if not command:
        return []
    # Split on whitespace + shell metacharacters (but NOT backslash —
    # backslash is a path separator on Windows).
    tokens = re.split(r'[\s|&;()<>"\'`]+', command)
    paths: List[str] = []
    for tok in tokens:
        if not tok:
            continue
        if tok.startswith("-"):
            continue
        if tok in {"if", "then", "else", "fi", "do", "done", "for", "in", "while", "and", "or", "not"}:
            continue
        is_path_like = (
            tok.startswith("/")
            or tok.startswith("~/")
            or tok.startswith("./")
            or tok.startswith("../")
            or "/" in tok
            or "\\" in tok
            or (len(tok) >= 2 and tok[1] == ":")  # Windows drive letter like C:
            or tok in {".", ".."}
        )
        if not is_path_like:
            continue
        # Strip trailing punctuation that may have leaked through (e.g.
        # ``cp /tmp/bar.`` — the ``.`` is end-of-sentence, not a path).
        tok = tok.rstrip(".,:;")
        if tok:
            paths.append(tok)
    return paths


def _path_under_skill_root(path: str, skill_dirs: List[Path]) -> Optional[Path]:
    """If *path* resolves to somewhere under one of *skill_dirs*, return
    the resolved Path. Otherwise None."""
    if not path:
        return None
    try:
        expanded = os.path.expanduser(path)
        resolved = Path(expanded).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    for d in skill_dirs:
        try:
            d_resolved = d.resolve()
            # Will raise ValueError if not under
            rel = resolved.relative_to(d_resolved)
            # Reject the root itself; we want paths INSIDE a skill
            if str(rel) in (".", ""):
                continue
            return resolved
        except (ValueError, OSError):
            continue
        except Exception:
            continue
    return None


def _check_terminal_skill_mutation(
    *,
    args: Optional[Dict[str, Any]],
    state: Dict[str, Any],
    tool_call_id: str,
    session_id: str,
) -> Optional[Dict[str, Any]]:
    """If a curator-context terminal call mutates a skill path, apply the
    same dry-run / approval-gate rules as skill_manage would.

    Returns:
      - ``{"action": "block"}`` in dry-run mode when the command mutates
        any path under a skill directory.
      - ``{"action": "approve", ...}`` in real execution so the existing
        ``tools.approval.request_tool_approval`` gate can prompt the
        human. The LLM should not have used terminal for skill mutation
        in the first place — it bypasses content validation.
      - ``None`` when the command is non-mutating or doesn't touch skill
        paths (legitimate terminal use is allowed).
    """
    if not isinstance(args, dict):
        return None
    command = args.get("command") or ""
    if not isinstance(command, str) or not command.strip():
        return None

    if not _is_mutating_shell_command(command):
        return None

    # Resolve skill directories. Fail open (return None) if the helper
    # can't be imported — better to allow and log than crash the hook.
    try:
        from agent.skill_utils import get_all_skills_dirs
        skill_dirs = [Path(d) for d in (get_all_skills_dirs() or [])]
    except Exception as e:
        logger.debug("_check_terminal_skill_mutation: get_all_skills_dirs failed: %s", e)
        skill_dirs = []

    if not skill_dirs:
        # If we can't determine skill roots, we can't safely gate. Log
        # to audit and allow (a curator without skill dir info shouldn't
        # block all terminal calls).
        _write_audit({
            "ts": datetime.now(timezone.utc).isoformat(),
            "hook": "pre_tool_call",
            "verdict": "allow_terminal_no_skill_dirs",
            "tool": "terminal",
            "command_preview": command[:_AUDIT_ARG_TRUNCATE],
            "tool_call_id": tool_call_id,
            "session_id": session_id,
            "note": "could not resolve skill directories; terminal check skipped",
        })
        return None

    hit_paths: List[Tuple[str, Path]] = []
    for token in _extract_paths_from_command(command):
        sp = _path_under_skill_root(token, skill_dirs)
        if sp is not None:
            hit_paths.append((token, sp))

    if not hit_paths:
        return None

    target_display = ", ".join(t for t, _ in hit_paths)

    if state.get("dry_run"):
        message = (
            f"[curator-guard] DRY-RUN mode is active: terminal command "
            f"that mutates skill path(s) ({target_display}) is BLOCKED. "
            f"Use skill_manage instead of terminal for skill mutations. "
            f"(Bug #2 fix: terminal was bypassing the curator dry-run guard.)"
        )
        _write_audit({
            "ts": datetime.now(timezone.utc).isoformat(),
            "hook": "pre_tool_call",
            "verdict": "block_dry_run_terminal",
            "tool": "terminal",
            "command_preview": command[:_AUDIT_ARG_TRUNCATE],
            "skill_paths": [str(p) for _, p in hit_paths],
            "tool_call_id": tool_call_id,
            "session_id": session_id,
            "message": message,
        })
        return {"action": "block", "message": message}

    # Real execution: escalate to approval gate
    message = (
        f"[curator-guard] terminal command mutates skill path(s) "
        f"({target_display}). The curator should use skill_manage for "
        f"skill mutations; using terminal bypasses content validation. "
        f"Escalating to human-approval gate."
    )
    _write_audit({
        "ts": datetime.now(timezone.utc).isoformat(),
        "hook": "pre_tool_call",
        "verdict": "approve_terminal_skill_mutation",
        "tool": "terminal",
        "command_preview": command[:_AUDIT_ARG_TRUNCATE],
        "skill_paths": [str(p) for _, p in hit_paths],
        "tool_call_id": tool_call_id,
        "session_id": session_id,
        "message": message,
    })
    return {
        "action": "approve",
        "message": message,
        "rule_key": "curator_guard:terminal:skill_mutation",
    }
