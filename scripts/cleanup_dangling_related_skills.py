"""One-off cleanup: drop ``related_skills`` entries that point to non-existent
skills. The bug was that 16 entries across the bundled skills pointed at
skill names that never shipped (e.g. ``subagent-driven-development``,
``excalidraw``, ``debugging-hermes-tui-commands``), so the learning graph
and skill recommenders would silently generate edges to ghosts.

This script is idempotent — re-running it on already-clean skills is a no-op.
Use ``scripts/check_skill_related.py`` to verify the cleanup or gate CI.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


def _load_frontmatter(text: str):
    """Return (frontmatter_dict, body) or (None, None) on parse failure."""
    if not text.startswith("---"):
        return None, None
    end = text.find("\n---", 3)
    if end == -1:
        return None, None
    try:
        fm = yaml.safe_load(text[3:end])
    except Exception:
        return None, None
    body = text[end + 4:]
    return (fm if isinstance(fm, dict) else {}), body


def _related_in(fm: dict) -> list:
    """Return the related_skills list regardless of nesting shape.

    Supports both top-level ``related_skills`` and
    ``metadata.hermes.related_skills``.
    """
    if isinstance(fm.get("related_skills"), list):
        return [x for x in fm["related_skills"] if isinstance(x, str)]
    meta = fm.get("metadata")
    if isinstance(meta, dict):
        hermes = meta.get("hermes")
        if isinstance(hermes, dict) and isinstance(hermes.get("related_skills"), list):
            return [x for x in hermes["related_skills"] if isinstance(x, str)]
    return []


def _set_related(fm: dict, related: list) -> None:
    """Persist *related* back into the same nesting shape we read it from."""
    if "related_skills" in fm:
        if related:
            fm["related_skills"] = related
        else:
            fm.pop("related_skills", None)
        return
    meta = fm.get("metadata")
    if isinstance(meta, dict):
        hermes = meta.get("hermes")
        if isinstance(hermes, dict) and "related_skills" in hermes:
            if related:
                hermes["related_skills"] = related
            else:
                hermes.pop("related_skills", None)


def _serialize(fm: dict, body: str) -> str:
    """Write frontmatter back, preserving the original ``---`` framing."""
    if not fm:
        return f"---\n{{}}\n---\n{body}"
    yaml_text = yaml.safe_dump(
        fm, default_flow_style=False, allow_unicode=True, sort_keys=False
    ).rstrip("\n")
    # Ensure the body starts with a newline (the original layout had
    # ``---\n<yaml>\n---\n<body>`` — the parser consumed the closing
    # ``---\n`` separator; we re-emit a single newline).
    prefix = "---\n" + yaml_text + "\n---\n"
    if body and not body.startswith("\n"):
        prefix += "\n"
    return prefix + body


def collect_existing_skills(skills_root: Path) -> set:
    """Return the set of skill ``name`` values declared anywhere under
    *skills_root*."""
    names = set()
    for p in sorted(skills_root.rglob("SKILL.md")):
        fm, _ = _load_frontmatter(p.read_text(encoding="utf-8"))
        if fm and isinstance(fm.get("name"), str):
            names.add(fm["name"])
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skills-root",
        type=Path,
        default=Path("skills"),
        help="Path to the skills root (default: ./skills)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be dropped without writing",
    )
    args = parser.parse_args()

    skills_root: Path = args.skills_root
    if not skills_root.is_dir():
        print(f"error: skills root not found: {skills_root}", file=sys.stderr)
        return 2

    existing = collect_existing_skills(skills_root)
    print(f"scanned {len(existing)} skills in {skills_root}")

    total_dropped = 0
    files_touched = 0
    for p in sorted(skills_root.rglob("SKILL.md")):
        text = p.read_text(encoding="utf-8")
        fm, body = _load_frontmatter(text)
        if fm is None:
            continue
        related = _related_in(fm)
        if not related:
            continue
        kept = [r for r in related if r in existing]
        dropped = [r for r in related if r not in existing]
        if not dropped:
            continue
        rel = p.relative_to(skills_root.parent)
        print(f"\n{rel}  ({fm.get('name', '?')})")
        for d in dropped:
            print(f"  drop dangling: {d}")
        if not args.dry_run:
            _set_related(fm, kept)
            new_text = _serialize(fm, body)
            if new_text != text:
                p.write_text(new_text, encoding="utf-8")
                files_touched += 1
                total_dropped += len(dropped)

    if args.dry_run:
        print(f"\n[dry-run] would drop entries; rerun without --dry-run to apply")
        return 0
    print(
        f"\ndone: dropped {total_dropped} dangling refs across "
        f"{files_touched} files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
