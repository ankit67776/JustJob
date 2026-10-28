"""Company boards found in bulk, one file per applicant tracking system: companies/<ats>.tsv.

Each line is `slug<TAB>Company name<TAB>open jobs when last checked`. `harvest` and
`discover --add` write these files; boards in config.toml are fetched too, and their names win.
"""
from __future__ import annotations

from pathlib import Path

from .config import ROOT

DIR = ROOT / "companies"
HEADER = "# slug\tcompany\topen jobs when last checked (written by `python -m justjob harvest`)\n"


def path(ats: str, base: Path | None = None) -> Path:
    return (base or DIR) / f"{ats}.tsv"


def load(ats: str, base: Path | None = None) -> dict[str, tuple[str, int]]:
    p = path(ats, base)
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        slug, name, jobs = (line.split("\t") + ["", ""])[:3]
        out[slug.strip()] = (name.strip(), int(jobs) if jobs.strip().isdigit() else 0)
    return out


def boards(ats: str, base: Path | None = None) -> dict[str, str]:
    return {slug: name or slug for slug, (name, _) in load(ats, base).items()}


def save(ats: str, rows: dict[str, tuple[str, int]], base: Path | None = None) -> None:
    p = path(ats, base)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [HEADER] + [f"{slug}\t{' '.join(name.split())}\t{jobs}\n" for slug, (name, jobs) in sorted(rows.items())]
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    tmp.replace(p)   # never leave a half-written file behind


def add(ats: str, found: dict[str, tuple[str, int]], base: Path | None = None) -> int:
    """Merge boards into the file; returns how many were new."""
    rows = load(ats, base)
    new = sum(1 for slug in found if slug not in rows)
    rows.update(found)
    save(ats, rows, base)
    return new
