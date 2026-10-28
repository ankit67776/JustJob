"""JSearch (on RapidAPI) - Google for Jobs results, which include many LinkedIn/Indeed/Glassdoor/
Naukri listings.

The free plan has a small monthly request allowance; each query costs one request per run.
"""
from __future__ import annotations

import logging
import os
from functools import partial

from ..models import BoardResult, Job
from ..net import get_json
from ..text import to_iso

NAME = "jsearch"
SNAPSHOT = False
API = "https://jsearch.p.rapidapi.com/search-v2"
HOST = "jsearch.p.rapidapi.com"
# Locations come as "Bonth, Odisha" plus a country code; the country name is appended so the
# location filter can match on it.
COUNTRIES = {
    "IN": "India", "US": "United States", "GB": "United Kingdom", "CA": "Canada", "AU": "Australia",
    "DE": "Germany", "FR": "France", "NL": "Netherlands", "SG": "Singapore", "AE": "United Arab Emirates",
    "IE": "Ireland",
}

log = logging.getLogger(__name__)


def tasks(sc: dict):
    key = os.environ.get("JSEARCH_API_KEY")
    if not key:
        log.info("jsearch: skipped, JSEARCH_API_KEY not set")
        return []
    return [(f"jsearch:{q}", partial(fetch_query, sc, key, q)) for q in sc.get("queries", [])]


def _location(p: dict) -> str:
    place = p.get("job_location") or ", ".join(x for x in (p.get("job_city"), p.get("job_state")) if x)
    code = (p.get("job_country") or "").upper()
    country = COUNTRIES.get(code, code)
    if country and country.lower() not in place.lower():
        place = f"{place}, {country}" if place else country
    return place


def _apply_link(p: dict) -> str:
    # Prefer the employer's own application page over a job board when Google lists one.
    direct = next((o.get("apply_link") for o in p.get("apply_options") or []
                   if o.get("is_direct") and o.get("apply_link")), None)
    return direct or p.get("job_apply_link") or p.get("job_google_link") or ""


def fetch_query(sc: dict, key: str, query: str, ctx) -> BoardResult:
    params = {"query": query, "page": 1, "num_pages": 1, "date_posted": sc.get("date_posted", "3days")}
    if sc.get("country"):
        params["country"] = sc["country"]
    data = get_json(API, params=params, headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": HOST}).get("data")
    postings = data.get("jobs", []) if isinstance(data, dict) else (data or [])
    jobs = []
    for p in postings:
        lo, hi = p.get("job_min_salary"), p.get("job_max_salary")
        salary = p.get("job_salary_string") or (
            f"{lo:,.0f}–{hi:,.0f} / {p.get('job_salary_period') or ''}".strip() if lo and hi else "")
        publisher = (p.get("job_publisher") or "").strip()
        jobs.append(Job(
            source=NAME,
            source_id=str(p["job_id"]),
            title=(p.get("job_title") or "").strip(),
            company=(p.get("employer_name") or "").strip(),
            url=_apply_link(p),
            location=_location(p),
            remote=p.get("job_is_remote"),
            description=p.get("job_description") or "",
            posted_at=to_iso(p.get("job_posted_at_datetime_utc") or p.get("job_posted_at_timestamp")),
            salary=salary or "",
            # Where Google found the job (LinkedIn, Indeed, ...), shown as "via LinkedIn (JSearch)".
            board=f"jsearch:{publisher}" if publisher else "jsearch",
        ))
    return BoardResult(board="jsearch", jobs=jobs, snapshot=False)
