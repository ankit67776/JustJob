"""Lever public postings API: https://github.com/lever/postings-api"""
from __future__ import annotations

import re
from functools import partial

from ..models import BoardResult, Job
from ..net import get_json
from ..text import html_to_text, looks_remote, to_iso

NAME = "lever"
SNAPSHOT = True
LINK = re.compile(r"jobs\.(?:eu\.)?lever\.co/([a-z0-9-]+)", re.I)
HOSTS = {"global": "https://api.lever.co", "eu": "https://api.eu.lever.co"}


def probe(slug: str) -> tuple[int, str]:
    return len(get_json(f"{HOSTS['global']}/v0/postings/{slug}", params={"mode": "json"})), ""


def tasks(sc: dict):
    out = []
    for key, region in (("boards", "global"), ("eu_boards", "eu")):
        for slug, name in sc.get(key, {}).items():
            out.append((f"lever:{slug}", partial(fetch_board, HOSTS[region], slug, name)))
    return out


def _description(p: dict) -> str:
    parts = [html_to_text(p.get("description") or p.get("descriptionBody"))]
    for section in p.get("lists") or []:
        parts.append(f"{section.get('text', '')}\n{html_to_text(section.get('content'))}")
    parts.append(html_to_text(p.get("additional")))
    return "\n\n".join(x for x in parts if x.strip())


def fetch_board(host: str, slug: str, name: str, ctx) -> BoardResult:
    postings = get_json(f"{host}/v0/postings/{slug}", params={"mode": "json"})
    jobs = []
    for p in postings:
        cats = p.get("categories") or {}
        locations = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
        location = "; ".join(locations)
        workplace = (p.get("workplaceType") or "").lower()
        remote = True if workplace == "remote" else (False if workplace else (looks_remote(location) or None))
        jobs.append(Job(
            source=NAME,
            source_id=f"{slug}/{p['id']}",
            title=(p.get("text") or "").strip(),
            company=name,
            url=p.get("hostedUrl") or p.get("applyUrl") or "",
            location=location,
            remote=remote,
            description=_description(p),
            posted_at=to_iso(p.get("createdAt")),
            salary=_salary(p.get("salaryRange")),
            board=f"lever:{slug}",
        ))
    return BoardResult(board=f"lever:{slug}", jobs=jobs, snapshot=True)


def _salary(r: dict | None) -> str:
    if not r or not r.get("min"):
        return ""
    return f"{r.get('currency', '')} {r['min']:,}–{r.get('max', r['min']):,} / {r.get('interval', '')}".strip()
