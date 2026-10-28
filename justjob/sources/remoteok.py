"""Remote OK public API: https://remoteok.com/api

Their terms ask for a link back to the Remote OK posting, which is the URL we store and show.
"""
from __future__ import annotations

from ..models import BoardResult, Job
from ..net import get_json
from ..text import html_to_text, to_iso

NAME = "remoteok"
SNAPSHOT = False
API = "https://remoteok.com/api"


def tasks(sc: dict):
    return [("remoteok", fetch)]


def fetch(ctx) -> BoardResult:
    data = get_json(API)
    jobs = []
    for p in data:
        if not isinstance(p, dict) or "id" not in p or not p.get("position"):
            continue   # the first element is the legal notice
        lo, hi = p.get("salary_min") or 0, p.get("salary_max") or 0
        jobs.append(Job(
            source=NAME,
            source_id=str(p["id"]),
            title=p["position"].strip(),
            company=(p.get("company") or "").strip(),
            url=p.get("url") or p.get("apply_url") or "",
            location=p.get("location") or "Remote",
            remote=True,
            description=html_to_text(p.get("description")),
            posted_at=to_iso(p.get("date") or p.get("epoch")),
            salary=(f"${lo:,}" if lo == hi else f"${lo:,}–${hi:,}") if lo and hi else "",
            board="remoteok",
        ))
    return BoardResult(board="remoteok", jobs=jobs, snapshot=False)
