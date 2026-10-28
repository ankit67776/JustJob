"""SmartRecruiters public postings API: https://developers.smartrecruiters.com/docs/posting-api"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from ..models import BoardResult, Job
from ..net import get_json
from ..text import html_to_text, to_iso

NAME = "smartrecruiters"
SNAPSHOT = True
API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
PAGE = 100
LINK = re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([a-z0-9-]+)", re.I)
MAX_POSTINGS = 3000

log = logging.getLogger(__name__)


def probe(slug: str) -> tuple[int, str]:
    page = get_json(API.format(slug=slug), params={"limit": 1})
    content = page.get("content") or []
    return page.get("totalFound", 0), ((content[0].get("company") or {}).get("name") or "") if content else ""


def tasks(sc: dict):
    return [(f"smartrecruiters:{slug}", partial(fetch_board, slug, name))
            for slug, name in sc.get("boards", {}).items()]


def _listing(slug: str) -> list[dict]:
    out: list[dict] = []
    while len(out) < MAX_POSTINGS:
        page = get_json(API.format(slug=slug), params={"limit": PAGE, "offset": len(out)})
        content = page.get("content") or []
        out += content
        if not content or len(out) >= page.get("totalFound", 0):
            break
    return out


def _to_job(slug: str, name: str, p: dict) -> Job:
    loc = p.get("location") or {}
    location = loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("country")) if x)
    return Job(
        source=NAME,
        source_id=f"{slug}/{p['id']}",
        title=(p.get("name") or "").strip(),
        company=name,
        url=f"https://jobs.smartrecruiters.com/{slug}/{p['id']}",
        location=location,
        remote=True if loc.get("remote") else (False if loc else None),
        posted_at=to_iso(p.get("releasedDate")),
        board=f"smartrecruiters:{slug}",
    )


def _description(d: dict) -> str:
    sections = (d.get("jobAd") or {}).get("sections") or {}
    parts = []
    level = (d.get("experienceLevel") or {}).get("label")
    if level and level.lower() != "not applicable":
        parts.append(f"Experience level: {level}")
    # companyDescription is boilerplate about the employer; skip it.
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        text = html_to_text((sections.get(key) or {}).get("text"))
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def fetch_board(slug: str, name: str, ctx) -> BoardResult:
    jobs = [_to_job(slug, name, p) for p in _listing(slug)]
    listed = [j.id for j in jobs]

    def detail(job: Job) -> Job | None:
        try:
            d = get_json(f"{API.format(slug=slug)}/{job.source_id.split('/', 1)[1]}")
        except Exception as e:   # retried on the next run
            log.debug("smartrecruiters:%s detail failed for %s: %s", slug, job.id, e)
            return None
        job.description = _description(d)
        job.url = d.get("postingUrl") or job.url
        return job

    with ThreadPoolExecutor(4) as ex:
        done = [j for j in ex.map(detail, [j for j in jobs if ctx.wants(j)]) if j and j.description]
    return BoardResult(board=f"smartrecruiters:{slug}", jobs=done, snapshot=True, listed_ids=listed)
