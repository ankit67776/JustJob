"""Adzuna search API: https://developer.adzuna.com/

Descriptions are truncated snippets, so experience checks on these jobs are often "unclear".
"""
from __future__ import annotations

import logging
import os
from functools import partial

from ..models import BoardResult, Job
from ..net import get_json
from ..text import html_to_text, looks_remote, to_iso

NAME = "adzuna"
SNAPSHOT = False
API = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"

log = logging.getLogger(__name__)


def tasks(sc: dict):
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        log.info("adzuna: skipped, ADZUNA_APP_ID / ADZUNA_APP_KEY not set")
        return []
    return [(f"adzuna:{q}", partial(fetch_query, sc, app_id, app_key, q)) for q in sc.get("queries", [])]


def fetch_query(sc: dict, app_id: str, app_key: str, query: str, ctx) -> BoardResult:
    params = {
        "app_id": app_id, "app_key": app_key, "what": query, "results_per_page": 50,
        "max_days_old": sc.get("max_days_old", 3), "content-type": "application/json",
    }
    data = get_json(API.format(country=sc.get("country", "us")), params=params)
    jobs = []
    for p in data.get("results", []):
        location = (p.get("location") or {}).get("display_name", "")
        title = html_to_text(p.get("title"))
        description = html_to_text(p.get("description"))
        lo, hi = p.get("salary_min"), p.get("salary_max")
        jobs.append(Job(
            source=NAME,
            source_id=str(p["id"]),
            title=title,
            company=((p.get("company") or {}).get("display_name") or "").strip(),
            url=p.get("redirect_url") or "",
            location=location,
            remote=looks_remote(f"{title} {location}") or None,
            description=description,
            posted_at=to_iso(p.get("created")),
            salary=f"{lo:,.0f}–{hi:,.0f}" if lo and hi else "",
            board="adzuna",
        ))
    return BoardResult(board="adzuna", jobs=jobs, snapshot=False)
