"""Workable public job widget API (apply.workable.com/<slug>)."""
from __future__ import annotations

import re
from functools import partial

from ..models import BoardResult, Job
from ..net import get_json
from ..text import html_to_text, to_iso

NAME = "workable"
SNAPSHOT = True
API = "https://apply.workable.com/api/v1/widget/accounts/{slug}"
CONCURRENCY = 2   # apply.workable.com answers HTTP 429 quickly
# ...and blocks for ~a day after a few thousand requests, so each run fetches this many boards
# (the least recently checked); the rest come round on later runs.
MAX_BOARDS = 300
LINK = re.compile(r"apply\.workable\.com/([a-z0-9-]+)", re.I)


def tasks(sc: dict):
    return [(f"workable:{slug}", partial(fetch_board, slug, name)) for slug, name in sc.get("boards", {}).items()]


def probe(slug: str) -> tuple[int, str]:
    data = get_json(API.format(slug=slug))
    return len(data.get("jobs", [])), data.get("name") or ""


def fetch_board(slug: str, name: str, ctx) -> BoardResult:
    data = get_json(API.format(slug=slug), params={"details": "true"})
    jobs = []
    for p in data.get("jobs", []):
        location = ", ".join(x for x in (p.get("city"), p.get("state"), p.get("country")) if x)
        jobs.append(Job(
            source=NAME,
            source_id=f"{slug}/{p.get('shortcode') or p.get('id')}",
            title=(p.get("title") or "").strip(),
            company=name,
            url=p.get("url") or p.get("application_url") or f"https://apply.workable.com/{slug}/",
            location=location,
            remote=bool(p.get("telecommuting")) or None,
            description=html_to_text(p.get("description")),
            posted_at=to_iso(p.get("published_on") or p.get("created_at")),
            board=f"workable:{slug}",
        ))
    return BoardResult(board=f"workable:{slug}", jobs=jobs, snapshot=True)
