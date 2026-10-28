"""Rippling ATS public job boards (ats.rippling.com/<slug>)."""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from ..models import BoardResult, Job
from ..net import get_json
from ..text import html_to_text, looks_remote, to_iso

NAME = "rippling"
SNAPSHOT = True
API = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"
LINK = re.compile(r"ats\.rippling\.com/(?:[a-z]{2}-[a-z]{2}/)?([a-z0-9-]+)", re.I)

log = logging.getLogger(__name__)


def tasks(sc: dict):
    return [(f"rippling:{slug}", partial(fetch_board, slug, name)) for slug, name in sc.get("boards", {}).items()]


def probe(slug: str) -> tuple[int, str]:
    return len(get_json(API.format(slug=slug))), ""


def to_job(slug: str, name: str, p: dict) -> Job:
    location = (p.get("workLocation") or {}).get("label") or ""
    return Job(
        source=NAME,
        source_id=f"{slug}/{p['uuid']}",
        title=" ".join((p.get("name") or "").split()),
        company=name,
        url=p.get("url") or f"https://ats.rippling.com/{slug}/jobs/{p['uuid']}",
        location=location,
        remote=looks_remote(location) or None,
        board=f"rippling:{slug}",
    )


def fill_details(job: Job, d: dict) -> None:
    # description.company is the same "about us" text on every posting; the role part is what matters.
    job.description = html_to_text((d.get("description") or {}).get("role"))
    locations = d.get("workLocations") or []
    if len(locations) > 1:
        job.location = "; ".join(locations)
        job.remote = looks_remote(job.location) or None
    job.posted_at = to_iso(d.get("createdOn"))


def fetch_board(slug: str, name: str, ctx) -> BoardResult:
    api = API.format(slug=slug)
    jobs = [to_job(slug, name, p) for p in get_json(api) if p.get("uuid")]
    listed = [j.id for j in jobs]

    def detail(job: Job) -> Job | None:
        try:
            fill_details(job, get_json(f"{api}/{job.source_id.split('/', 1)[1]}"))
        except Exception as e:   # retried on the next run
            log.debug("rippling:%s detail failed for %s: %s", slug, job.id, e)
            return None
        return job

    with ThreadPoolExecutor(4) as ex:
        done = [j for j in ex.map(detail, [j for j in jobs if ctx.wants(j)]) if j and j.description]
    return BoardResult(board=f"rippling:{slug}", jobs=done, snapshot=True, listed_ids=listed)
