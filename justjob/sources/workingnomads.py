"""Working Nomads public jobs feed: https://www.workingnomads.com/api/exposed_jobs/"""
from __future__ import annotations

import re

from ..models import BoardResult, Job
from ..net import get_json
from ..text import html_to_text, to_iso

NAME = "workingnomads"
SNAPSHOT = False
API = "https://www.workingnomads.com/api/exposed_jobs/"


def tasks(sc: dict):
    return [("workingnomads", fetch)]


def to_job(p: dict) -> Job | None:
    url = p.get("url") or ""
    m = re.search(r"/(\d+)/?$", url)
    if not (m and p.get("title")):
        return None
    location = (p.get("location") or "").strip()
    return Job(
        source=NAME,
        source_id=m.group(1),
        title=p["title"].strip(),
        company=(p.get("company_name") or "").strip(),
        url=url,
        location=f"Remote - {location}" if location else "Remote",
        remote=True,
        description=html_to_text(p.get("description")),
        posted_at=to_iso(p.get("pub_date")),
        board="workingnomads",
    )


def fetch(ctx) -> BoardResult:
    jobs = [j for p in get_json(API) if (j := to_job(p))]
    return BoardResult(board="workingnomads", jobs=jobs, snapshot=False)
