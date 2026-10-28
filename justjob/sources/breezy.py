"""Breezy HR public job feeds (<slug>.breezy.hr/json).

The feed has no descriptions, so these jobs are judged on title and location (the AI step sees
the title only).
"""
from __future__ import annotations

import re
from functools import partial

from ..models import BoardResult, Job
from ..net import get_json
from ..text import to_iso

NAME = "breezy"
SNAPSHOT = True
API = "https://{slug}.breezy.hr/json"
LINK = re.compile(r"(?<![a-z0-9-])([a-z0-9-]+)\.breezy\.hr", re.I)


def tasks(sc: dict):
    return [(f"breezy:{slug}", partial(fetch_board, slug, name)) for slug, name in sc.get("boards", {}).items()]


def probe(slug: str) -> tuple[int, str]:
    data = get_json(API.format(slug=slug))
    company = (data[0].get("company") or {}).get("name", "") if data else ""
    return len(data), company


def fetch_board(slug: str, name: str, ctx) -> BoardResult:
    jobs = []
    for p in get_json(API.format(slug=slug)):
        loc = p.get("location") or {}
        jobs.append(Job(
            source=NAME,
            source_id=f"{slug}/{p['id']}",
            title=(p.get("name") or "").strip(),
            company=name,
            url=p.get("url") or f"https://{slug}.breezy.hr/",
            location=loc.get("name") or (loc.get("country") or {}).get("name", ""),
            remote=bool(loc.get("is_remote")) or None,
            posted_at=to_iso(p.get("published_date")),
            salary=p.get("salary") or "",
            board=f"breezy:{slug}",
        ))
    return BoardResult(board=f"breezy:{slug}", jobs=jobs, snapshot=True)
