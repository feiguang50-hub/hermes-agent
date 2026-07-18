"""Lint: fail if any bundled ``SKILL.md`` references a skill that doesn't exist.

Used in CI to keep ``related_skills`` honest. Exit codes:

* 0 — every reference points to a real skill.
* 1 — at least one dangling reference was found.
* 2 — invocation error (skills root missing, parse failure, etc).

Pair with ``cleanup_dangling_related_skills.py`` to apply fixes.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


def _load_frontmatter(text: str):
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    try:
        fm = yaml.safe_load(text[3:end])
    except Exception:
        return None
    return fm if isinstance(fm, dict) else {}


def _related_in(fm: dict) -> list:
    if isinstance(fm.get("related_skills"), list):
        return [x for x in fm["related_skills"] if isinstance(x, str)]
    meta = fm.get("metadata")
    if isinstance(meta, dict):
        hermes = meta.get("hermes")
        if isinstance(hermes, dict) and isinstance(hermes.get("related_skills"), list):
            return [x for x in hermes["related_skills"] if isinstance(x, str)]
    return []


def collect_existing_skills(skills_root: Path) -> set:
    names = set()
    for p in sorted(skills_root.rglob("SKILL.md")):
        fm = _load_frontmatter(p.read_text(encoding="utf-8"))
        if fm and isinstance(fm.get("name"), str):
            names.add(fm["name"])
    return names


def find_dangling(skills_root: Path) -> list:
    """Return list of (relpath, skill_name, dangling_ref) tuples."""
    existing = collect_existing_skills(skills_root)
    out = []
    for p in sorted(skills_root.rglob("SKILL.md")):
        fm = _load_frontmatter(p.read_text(encoding="utf-8"))
        if not fm:
            continue
        name = fm.get("name")
        if not isinstance(name, str):
            continue
        for r in _related_in(fm):
            if r not in existing:
                out.append((str(p.relative_to(skills_root.parent)), name, r))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skills-root",
        type=Path,
        default=Path("skills"),
        help="Path to the skills root (default: ./skills)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-file dangling references",
    )
    args = parser.parse_args()

    if not args.skills_root.is_dir():
        print(f"error: skills root not found: {args.skills_root}", file=sys.stderr)
        return 2

    dangling = find_dangling(args.skills_root)
    if not dangling:
        print(f"OK: no dangling related_skills refs in {args.skills_root}")
        return 0

    print(f"FAIL: {len(dangling)} dangling related_skills refs found", file=sys.stderr)
    if args.verbose:
        from collections import defaultdict
        by_ref = defaultdict(list)
        for path, skill, ref in dangling:
            by_ref[ref].append((path, skill))
        for ref, hits in sorted(by_ref.items()):
            print(f"\n  '{ref}' (referenced by {len(hits)} skills, none exist):", file=sys.stderr)
            for path, skill in hits:
                print(f"    - {path}  ({skill})", file=sys.stderr)
        print(
            "\nFix with: python scripts/cleanup_dangling_related_skills.py",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
