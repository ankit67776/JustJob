"""Personio public job XML feeds (<slug>.jobs.personio.de/xml)."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from functools import partial

from ..models import BoardResult, Job
from ..net import request
from ..text import html_to_text, looks_remote, to_iso

NAME = "personio"
SNAPSHOT = True
FEED = "https://{slug}.jobs.personio.de/xml"
LINK = re.compile(r"(?<![a-z0-9-])([a-z0-9-]+)\.jobs\.personio\.(?:de|com)", re.I)


def tasks(sc: dict):
    return [(f"personio:{slug}", partial(fetch_board, slug, name)) for slug, name in sc.get("boards", {}).items()]


def _positions(slug: str) -> list[ET.Element]:
    return list(ET.fromstring(request(FEED.format(slug=slug))).iter("position"))


def probe(slug: str) -> tuple[int, str]:
    positions = _positions(slug)
    return len(positions), (positions[0].findtext("subcompany") or "").strip() if positions else ""


def fetch_board(slug: str, name: str, ctx) -> BoardResult:
    jobs = []
    for p in _positions(slug):
        pid = (p.findtext("id") or "").strip()
        if not pid:
            continue
        offices = [p.findtext("office") or ""] + [o.text or "" for o in p.iter("additionalOffice")]
        location = "; ".join(o.strip() for o in offices if o.strip())
        sections = []
        for d in p.iter("jobDescription"):
            heading, body = (d.findtext("name") or "").strip(), html_to_text(d.findtext("value"))
            sections.append(f"{heading}\n{body}" if heading else body)
        years = (p.findtext("yearsOfExperience") or "").strip()
        if years:
            sections.insert(0, f"Experience: {years.replace('-', ' to ')} years")
        jobs.append(Job(
            source=NAME,
            source_id=f"{slug}/{pid}",
            title=(p.findtext("name") or "").strip(),
            company=name,
            url=f"https://{slug}.jobs.personio.de/job/{pid}",
            location=location,
            remote=looks_remote(location) or None,
            description="\n\n".join(s for s in sections if s),
            posted_at=to_iso(p.findtext("createdAt")),
            board=f"personio:{slug}",
        ))
    return BoardResult(board=f"personio:{slug}", jobs=jobs, snapshot=True)
