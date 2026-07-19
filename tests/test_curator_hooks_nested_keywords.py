"""Tests for #16: keyword-retention guard on nested-category / CJK skills.

Regression guard for a real-data finding: `_load_skill_keywords` used to
resolve only the flat path `<skills_dir>/<name>`, so for category-nested
skills (`<skills_dir>/<category>/<skill>/SKILL.md` — the real-world norm) it
fell back to a name-only keyword set, making the retention check inert. The
fix adds a nested-aware fallback via `skill_usage._find_skill_dir`.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def iso_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    import tools.skill_usage as su
    importlib.reload(su)
    from agent import curator_hooks as ch
    importlib.reload(ch)
    return home, ch


def _write_nested_skill(home, category, name, description, body="# body\n\nsome text\n"):
    d = home / "skills" / category / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\ncategory: {category}\n---\n\n{body}",
        encoding="utf-8",
    )
    return d


def test_nested_skill_keywords_are_extracted_not_name_only(iso_home):
    """A nested-category skill must yield its description keywords, not just
    the name variants. Before the fix this returned name-only."""
    home, ch = iso_home
    _write_nested_skill(
        home, "devtools", "svc-helper",
        "Retry backoff jitter timeout for flaky upstream requests.",
    )
    kws = ch._load_skill_keywords("svc-helper")
    # name variants only would be {'svc-helper','svc_helper'} → 2 items
    assert len(kws) > 2, f"expected description keywords, got name-only: {kws}"
    for expected in ("retry", "backoff", "jitter", "timeout"):
        assert expected in kws, f"{expected!r} missing from nested-skill keywords: {kws}"


def test_nested_skill_retention_check_fires(iso_home):
    """End-to-end: for a nested skill, a patch that keeps the name but drops
    the real keywords must escalate to approval (retention < 50%), while a
    patch that preserves them passes. Before the fix, name-only keywords let
    the gutting patch through."""
    home, ch = iso_home
    _write_nested_skill(
        home, "devtools", "svc-helper",
        "Retry backoff jitter timeout for flaky upstream requests.",
    )
    audit = home / "audit.jsonl"

    # (a) gutting patch — keeps the name, drops every real keyword
    ch.enter_curator_context(dry_run=False, audit_log_path=audit)
    try:
        verdict = ch.curator_pre_tool_call_hook(
            tool_name="skill_manage",
            args={"action": "patch", "name": "svc-helper",
                  "new_string": "svc-helper now documents gardening tomatoes and roses"},
            tool_call_id="tc_gut", session_id="s1",
        )
    finally:
        ch.exit_curator_context()
    assert verdict is not None and verdict.get("action") == "approve", (
        f"gutting a nested skill should escalate to approval, got {verdict}"
    )

    # (b) faithful patch — preserves the real keywords → allowed
    ch.enter_curator_context(dry_run=False, audit_log_path=audit)
    try:
        verdict2 = ch.curator_pre_tool_call_hook(
            tool_name="skill_manage",
            args={"action": "patch", "name": "svc-helper",
                  "new_string": ("svc-helper handles retry with backoff, jitter, "
                                 "timeout for flaky upstream requests")},
            tool_call_id="tc_keep", session_id="s1",
        )
    finally:
        ch.exit_curator_context()
    assert verdict2 is None, f"faithful patch should pass, got {verdict2}"


def test_nested_cjk_skill_keywords_resolved(iso_home):
    """A nested skill with a Chinese description must still resolve its path
    and extract CJK keywords (not name-only). Documents that path resolution
    works for CJK; per-char tokenization quality is tracked separately (P4)."""
    home, ch = iso_home
    _write_nested_skill(
        home, "ai-music", "music-knowledge-rag",
        "小荧音乐知识库RAG的搭建与维护，音乐风格分析与和弦进行查询。",
    )
    kws = ch._load_skill_keywords("music-knowledge-rag")
    assert len(kws) > 2, f"nested CJK skill fell back to name-only: {kws}"
    # at least some CJK single-char tokens should be present
    assert any(any("一" <= c <= "鿿" for c in k) for k in kws), (
        f"expected CJK tokens from the description, got {kws}"
    )
