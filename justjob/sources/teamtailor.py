"""Teamtailor career sites: a public JSON Feed at <slug>.teamtailor.com/jobs.json."""
from __future__ import annotations

import re
from functools import partial

from ..models import BoardResult, Job
from ..net import get_json
from ..text import html_to_text, looks_remote, to_iso

NAME = "teamtailor"
SNAPSHOT = True
FEED = "https://{slug}.teamtailor.com/jobs.json"
LINK = re.compile(r"(?<![a-z0-9-])([a-z0-9-]+)\.teamtailor\.com", re.I)


def tasks(sc: dict):
    return [(f"teamtailor:{slug}", partial(fetch_board, slug, name)) for slug, name in sc.get("boards", {}).items()]


def probe(slug: str) -> tuple[int, str]:
    feed = get_json(FEED.format(slug=slug))
    return len(feed.get("items") or []), feed.get("title") or ""


def _location(posting: dict) -> str:
    places = posting.get("jobLocation") or []
    if isinstance(places, dict):
        places = [places]
    out = []
    for place in places:
        a = (place or {}).get("address") or {}
        text = ", ".join(x for x in (a.get("addressLocality"), a.get("addressRegion"), a.get("addressCountry")) if x)
        if text and text not in out:
            out.append(text)
    return "; ".join(out)


def to_job(slug: str, name: str, item: dict) -> Job:
    posting = item.get("_jobposting") or {}
    location = _location(posting)
    if posting.get("jobLocationType") == "TELECOMMUTE":
        location = f"Remote - {location}" if location else "Remote"
    ident = (posting.get("identifier") or {}).get("value") or item.get("id")
    return Job(
        source=NAME,
        source_id=f"{slug}/{ident}",
        title=(item.get("title") or "").strip(),
        company=name,
        url=item.get("url") or f"https://{slug}.teamtailor.com/jobs",
        location=location,
        remote=looks_remote(location) or None,
        description=html_to_text(item.get("content_html") or posting.get("description")),
        posted_at=to_iso(item.get("date_published") or posting.get("datePosted")),
        board=f"teamtailor:{slug}",
    )


def fetch_board(slug: str, name: str, ctx) -> BoardResult:
    items = get_json(FEED.format(slug=slug)).get("items") or []
    return BoardResult(board=f"teamtailor:{slug}", jobs=[to_job(slug, name, i) for i in items], snapshot=True)
