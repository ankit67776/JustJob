"""Himalayas remote jobs search API (https://himalayas.app/jobs/api/search)."""
from __future__ import annotations

from functools import partial

from ..models import BoardResult, Job
from ..net import get_json
from ..text import html_to_text, to_iso

NAME = "himalayas"
SNAPSHOT = False
API = "https://himalayas.app/jobs/api/search"


def tasks(sc: dict):
    return [(f"himalayas:{q}", partial(fetch_query, sc, q)) for q in sc.get("queries", [])]


def _salary(p: dict) -> str:
    lo, hi = p.get("minSalary"), p.get("maxSalary")
    if not (lo and hi):
        return ""
    return f"{p.get('currency') or ''} {lo:,}–{hi:,} / {p.get('salaryPeriod') or 'year'}".strip()


def to_job(p: dict) -> Job | None:
    guid = p.get("guid") or p.get("applicationLink") or ""
    if not guid or not p.get("title"):
        return None
    regions = p.get("locationRestrictions") or []
    seniority = ", ".join(p.get("seniority") or [])
    description = html_to_text(p.get("description"))
    if seniority:
        description = f"Seniority: {seniority}\n\n{description}"
    return Job(
        source=NAME,
        source_id=guid.split("/companies/", 1)[-1].strip("/"),
        title=p["title"].strip(),
        company=(p.get("companyName") or "").strip(),
        url=p.get("applicationLink") or guid,
        location=f"Remote - {', '.join(regions)}" if regions else "Remote (worldwide)",
        remote=True,
        description=description,
        posted_at=to_iso(p.get("pubDate")),
        salary=_salary(p),
        board="himalayas",
    )


def fetch_query(sc: dict, query: str, ctx) -> BoardResult:
    params = {"q": query, "sort": "recent"}
    if sc.get("country"):
        params["country"] = sc["country"]
    data = get_json(API, params=params)
    jobs = [j for p in data.get("jobs", []) if (j := to_job(p))]
    return BoardResult(board="himalayas", jobs=jobs, snapshot=False)
