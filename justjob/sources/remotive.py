"""Remotive public API: https://remotive.com/api-documentation

Their terms ask for at most a few requests a day and a link back to the Remotive posting, which
is the URL we store and show.
"""
from __future__ import annotations

from functools import partial

from ..models import BoardResult, Job
from ..net import get_json
from ..text import html_to_text, to_iso

NAME = "remotive"
SNAPSHOT = False   # the free feed only shows a recent slice
API = "https://remotive.com/api/remote-jobs"


def tasks(sc: dict):
    return [(f"remotive:{c}", partial(fetch_category, c)) for c in sc.get("categories", ["software-development"])]


def fetch_category(category: str, ctx) -> BoardResult:
    data = get_json(API, params={"category": category})
    jobs = [
        Job(
            source=NAME,
            source_id=str(p["id"]),
            title=(p.get("title") or "").strip(),
            company=(p.get("company_name") or "").strip(),
            url=p.get("url") or "",
            location=p.get("candidate_required_location") or "Remote",
            remote=True,
            description=html_to_text(p.get("description")),
            posted_at=to_iso(p.get("publication_date")),
            salary=p.get("salary") or "",
            board="remotive",
        )
        for p in data.get("jobs", [])
    ]
    return BoardResult(board="remotive", jobs=jobs, snapshot=False)
