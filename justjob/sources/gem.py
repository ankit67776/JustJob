"""Gem public job boards (jobs.gem.com/<slug>)."""
from __future__ import annotations

import re
from functools import partial

from ..models import BoardResult, Job
from ..net import get_json
from ..text import html_to_text, looks_remote, to_iso

NAME = "gem"
SNAPSHOT = True
API = "https://api.gem.com/job_board/v0/{slug}/job_posts/"
LINK = re.compile(r"jobs\.gem\.com/([a-z0-9-]+)", re.I)


def tasks(sc: dict):
    return [(f"gem:{slug}", partial(fetch_board, slug, name)) for slug, name in sc.get("boards", {}).items()]


def probe(slug: str) -> tuple[int, str]:
    return len(get_json(API.format(slug=slug))), ""


def to_job(slug: str, name: str, p: dict) -> Job:
    location = (p.get("location") or {}).get("name") or ""
    offices = [(o.get("location") or {}).get("name") or o.get("name") or "" for o in p.get("offices") or []]
    if len([o for o in offices if o]) > 1:
        location = "; ".join(o for o in offices if o)
    kind = (p.get("location_type") or "").lower()
    remote = (kind == "remote") if kind else (looks_remote(location) or None)
    return Job(
        source=NAME,
        source_id=f"{slug}/{p['id']}",
        title=(p.get("title") or "").strip(),
        company=name,
        url=p.get("absolute_url") or f"https://jobs.gem.com/{slug}/{p['id']}",
        location=location,
        remote=remote,
        description=p.get("content_plain") or html_to_text(p.get("content")),
        posted_at=to_iso(p.get("first_published_at") or p.get("created_at")),
        board=f"gem:{slug}",
    )


def fetch_board(slug: str, name: str, ctx) -> BoardResult:
    posts = get_json(API.format(slug=slug))
    return BoardResult(board=f"gem:{slug}", jobs=[to_job(slug, name, p) for p in posts if p.get("id")], snapshot=True)
