"""Hacker News "Ask HN: Who is hiring?" via the free Algolia HN API.

Each top-level comment is one company's post. The first line is usually
"Company | Role(s) | Location | Remote/Onsite | ..." but the format isn't enforced, so the
parsing here is best-effort; the AI step cleans up the rest.
"""
from __future__ import annotations

import re

from ..models import BoardResult, Job
from ..net import get_json
from ..text import html_to_text, looks_remote, to_iso

NAME = "hackernews"
SNAPSHOT = False
SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
ITEM = "https://hn.algolia.com/api/v1/items/{id}"

_ROLE = re.compile(r"engineer|developer|scientist|programmer|\bswe\b|\bsde\b|\bmle\b|technical staff|founding", re.I)
_URL = re.compile(r"https?://\S+")


def tasks(sc: dict):
    return [("hackernews", fetch)]


def latest_thread() -> dict | None:
    hits = get_json(SEARCH, params={"tags": "story,author_whoishiring", "hitsPerPage": 10}).get("hits", [])
    for h in hits:
        if (h.get("title") or "").lower().startswith("ask hn: who is hiring"):
            return h
    return None


def parse_comment(c: dict) -> Job | None:
    raw = c.get("text") or ""
    if not raw or c.get("author") is None:
        return None
    first = html_to_text(raw.split("<p>", 1)[0]).replace("\n", " ").strip()
    if not first:
        return None
    parts = [p.strip() for p in first.split("|")]
    if len(parts) == 1:
        parts = [p.strip() for p in re.split(r"\s+[—–-]\s+", first)]
    parts = [p for p in parts if p and not _URL.fullmatch(p)]
    if not parts:
        return None

    company = parts[0]
    paren = re.search(r"\(([^)]*)\)", company)
    extra_location = paren.group(1) if paren else ""
    company = re.sub(r"\s*\([^)]*\)", "", company).strip()[:80] or "Unknown"

    rest = parts[1:]
    roles = [p for p in rest if _ROLE.search(p)]
    if not roles and _ROLE.search(parts[0]):
        roles = [parts[0]]
    others = [p for p in rest if p not in roles]
    title = ", ".join(roles)[:200] if roles else first[:120]
    location = " | ".join(x for x in [extra_location, *others] if x)[:200]

    return Job(
        source=NAME,
        source_id=str(c["id"]),
        title=title,
        company=company,
        url=f"https://news.ycombinator.com/item?id={c['id']}",
        location=location,
        remote=looks_remote(first) or None,
        description=html_to_text(raw),
        posted_at=to_iso(c.get("created_at")),
        board="hackernews",
    )


def fetch(ctx) -> BoardResult:
    thread = latest_thread()
    if not thread:
        return BoardResult(board="hackernews", jobs=[], snapshot=False)
    item = get_json(ITEM.format(id=thread["objectID"]))
    jobs = [j for c in item.get("children", []) if (j := parse_comment(c))]
    return BoardResult(board="hackernews", jobs=jobs, snapshot=False)
