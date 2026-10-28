"""Greenhouse public job board API: https://developers.greenhouse.io/job-board.html"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from ..models import BoardResult, Job
from ..net import get_json
from ..text import html_to_text, looks_remote, to_iso

NAME = "greenhouse"
SNAPSHOT = True
API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
# Above this many jobs needing descriptions, one big `content=true` request beats many small ones.
BULK_THRESHOLD = 15
LINK = re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9-]+)", re.I)

log = logging.getLogger(__name__)


def probe(slug: str) -> tuple[int, str]:
    jobs = get_json(API.format(slug=slug)).get("jobs", [])
    return len(jobs), (jobs[0].get("company_name") or "") if jobs else ""


def tasks(sc: dict):
    return [(f"greenhouse:{slug}", partial(fetch_board, slug, name))
            for slug, name in sc.get("boards", {}).items()]


def _location_type(j: dict) -> str:
    for m in j.get("metadata") or []:
        if (m.get("name") or "").lower() in ("location type", "workplace type", "remote"):
            return str(m.get("value") or "").lower()
    return ""


_VAGUE_LOCATION = re.compile(r"\s*(?:hybrid|remote|on-?site|multiple locations?|various|flexible)?\s*", re.I)


def _fill_details(job: Job, raw: dict) -> None:
    job.description = html_to_text(raw.get("content"))
    # Some boards put only "Hybrid" in the location; the offices list has the actual cities.
    if _VAGUE_LOCATION.fullmatch(job.location):
        offices = "; ".join(o.get("location") or o.get("name") or "" for o in raw.get("offices") or [])
        if offices.strip("; "):
            job.location = f"{job.location.strip()} - {offices}" if job.location.strip() else offices


def _to_job(slug: str, name: str, j: dict) -> Job:
    location = (j.get("location") or {}).get("name", "") or ""
    loc_type = _location_type(j)
    remote = True if "remote" in loc_type else (False if loc_type else (looks_remote(location) or None))
    return Job(
        source=NAME,
        source_id=f"{slug}/{j['id']}",
        title=(j.get("title") or "").strip(),
        company=name or j.get("company_name") or slug,
        url=j.get("absolute_url") or f"https://job-boards.greenhouse.io/{slug}/jobs/{j['id']}",
        location=location,
        remote=remote,
        description=html_to_text(j.get("content")),
        posted_at=to_iso(j.get("first_published") or j.get("updated_at")),
        board=f"greenhouse:{slug}",
    )


def fetch_board(slug: str, name: str, ctx) -> BoardResult:
    url = API.format(slug=slug)
    listing = get_json(url).get("jobs", [])
    jobs = [_to_job(slug, name, j) for j in listing]
    listed = [j.id for j in jobs]
    wanted = [j for j in jobs if ctx.wants(j)]

    if len(wanted) > BULK_THRESHOLD:
        full = {str(j["id"]): j for j in get_json(url, params={"content": "true"}).get("jobs", [])}
        for job in wanted:
            raw = full.get(job.source_id.split("/", 1)[1])
            if raw:
                _fill_details(job, raw)
        done = [j for j in wanted if j.description]
    else:
        def detail(job: Job) -> Job | None:
            try:
                raw = get_json(f"{url}/{job.source_id.split('/', 1)[1]}")
            except Exception as e:   # keep going; the job is retried on the next run
                log.debug("greenhouse:%s detail failed for %s: %s", slug, job.id, e)
                return None
            _fill_details(job, raw)
            return job

        with ThreadPoolExecutor(4) as ex:
            done = [j for j in ex.map(detail, wanted) if j]

    # Only jobs whose description we got are returned for storing; everything listed counts as
    # still open.
    return BoardResult(board=f"greenhouse:{slug}", jobs=done, snapshot=True, listed_ids=listed)
