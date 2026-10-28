"""Recruitee public careers API (<slug>.recruitee.com/api/offers/)."""
from __future__ import annotations

import re
from functools import partial

from ..models import BoardResult, Job
from ..net import get_json
from ..text import html_to_text, to_iso

NAME = "recruitee"
SNAPSHOT = True
API = "https://{slug}.recruitee.com/api/offers/"
LINK = re.compile(r"(?<![a-z0-9-])([a-z0-9-]+)\.recruitee\.com", re.I)


def tasks(sc: dict):
    return [(f"recruitee:{slug}", partial(fetch_board, slug, name)) for slug, name in sc.get("boards", {}).items()]


def probe(slug: str) -> tuple[int, str]:
    offers = get_json(API.format(slug=slug)).get("offers", [])
    return len(offers), (offers[0].get("company_name") or "") if offers else ""


def fetch_board(slug: str, name: str, ctx) -> BoardResult:
    jobs = []
    for p in get_json(API.format(slug=slug)).get("offers", []):
        if p.get("status") not in (None, "published"):
            continue
        parts = [html_to_text(p.get("description")), html_to_text(p.get("requirements"))]
        jobs.append(Job(
            source=NAME,
            source_id=f"{slug}/{p['id']}",
            title=(p.get("title") or "").strip(),
            company=name,
            url=p.get("careers_url") or f"https://{slug}.recruitee.com/o/{p.get('slug', '')}",
            location=p.get("location") or ", ".join(x for x in (p.get("city"), p.get("country")) if x),
            remote=bool(p.get("remote")) or None,
            description="\n\n".join(x for x in parts if x),
            posted_at=to_iso(p.get("published_at") or p.get("created_at")),
            board=f"recruitee:{slug}",
        ))
    return BoardResult(board=f"recruitee:{slug}", jobs=jobs, snapshot=True)
