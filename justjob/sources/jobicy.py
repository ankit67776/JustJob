"""Jobicy remote jobs API: https://jobi.cy/apidocs

Their terms ask for credit with a link to the original posting, which is the URL we store.
"""
from __future__ import annotations

from functools import partial

from ..models import BoardResult, Job
from ..net import get_json
from ..text import html_to_text, to_iso

NAME = "jobicy"
SNAPSHOT = False
API = "https://jobicy.com/api/v2/remote-jobs"


def tasks(sc: dict):
    return [(f"jobicy:{t}", partial(fetch_tag, t)) for t in sc.get("tags", [])]


def to_job(p: dict) -> Job:
    geo = (p.get("jobGeo") or "").strip()
    level = (p.get("jobLevel") or "").strip()
    description = html_to_text(p.get("jobDescription"))
    if level and level.lower() != "any":
        description = f"Level: {level}\n\n{description}"
    lo, hi = p.get("annualSalaryMin"), p.get("annualSalaryMax")
    return Job(
        source=NAME,
        source_id=str(p["id"]),
        title=html_to_text(p.get("jobTitle")),
        company=(p.get("companyName") or "").strip(),
        url=p.get("url") or "",
        location=f"Remote - {geo}" if geo and geo.lower() != "anywhere" else "Remote (anywhere)",
        remote=True,
        description=description,
        posted_at=to_iso(p.get("pubDate")),
        salary=f"{p.get('salaryCurrency') or ''} {lo:,}–{hi:,} / year".strip() if lo and hi else "",
        board="jobicy",
    )


def fetch_tag(tag: str, ctx) -> BoardResult:
    data = get_json(API, params={"count": 50, "tag": tag})
    return BoardResult(board="jobicy", jobs=[to_job(p) for p in data.get("jobs", [])], snapshot=False)
