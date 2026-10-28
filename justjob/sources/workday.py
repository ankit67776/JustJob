"""Workday career sites (<tenant>.wd<N>.myworkdayjobs.com/<site>).

Workday has no official public API. This uses the JSON endpoints the public career site itself
calls (no login). Most large employers hire through Workday. A board's slug is
"<tenant>/<wdN>/<site>", e.g. "nvidia/wd5/nvidiaexternalcareersite".
"""
from __future__ import annotations

import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import partial

from ..models import BoardResult, Job
from ..net import get_json, post_json, request
from ..text import html_to_text, looks_remote, to_iso

NAME = "workday"
SNAPSHOT = True
CONCURRENCY = 12        # tenants are spread over many hosts
PAGE = 20               # the most the endpoint returns per request
MAX_POSTINGS = 1000     # newest first; bigger boards are fetched partially
LINK = re.compile(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[a-z]{2}/)?([a-z0-9_-]+)", re.I)
NOT_SITES = {"wday", "job", "login", "robots", "favicon", "assets"}   # "jobs" is a real site name (Red Hat, PayPal)
_MANY_LOCATIONS = re.compile(r"\d+ locations?", re.I)
_POSTED = re.compile(r"(\d+)\+? days? ago", re.I)
_CAREERS_AT = re.compile(r"careers? (?:at|@|with) (.+)", re.I)

log = logging.getLogger(__name__)


def slug_from(m: re.Match) -> str:
    site = m.group(3).lower()
    return "" if site in NOT_SITES else f"{m.group(1).lower()}/{m.group(2).lower()}/{site}"


def _host(slug: str) -> str:
    tenant, wd, _ = slug.split("/")
    return f"https://{tenant}.{wd}.myworkdayjobs.com"


def _api(slug: str) -> str:
    tenant, _, site = slug.split("/")
    return f"{_host(slug)}/wday/cxs/{tenant}/{site}"


def _page(slug: str, offset: int, limit: int = PAGE, query: str = "") -> dict:
    return post_json(f"{_api(slug)}/jobs", {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": query})


def search_query(terms) -> str:
    """'engineer OR developer OR "member of technical staff"' (Workday's search understands OR)."""
    return " OR ".join(f'"{t}"' if " " in t else t for t in terms)


def probe(slug: str) -> tuple[int, str]:
    total = _page(slug, 0, 1).get("total", 0)
    name = slug.split("/")[0].replace("-", " ").title()
    if total:
        try:   # the career page's title is often "Careers at <Company>"
            page = request(f"{_host(slug)}/{slug.split('/')[2]}", headers={"Accept": "text/html"})
            m = re.search(r'og:title" content="([^"]+)"', page.decode("utf-8", "replace"))
            m = m and _CAREERS_AT.search(html.unescape(m.group(1)))
            if m:
                name = m.group(1).strip()
        except Exception as e:
            log.debug("workday:%s name lookup failed: %s", slug, e)
    return total, name


def tasks(sc: dict):
    return [(f"workday:{slug}", partial(fetch_board, slug, name)) for slug, name in sc.get("boards", {}).items()]


def posted_on(text: str | None, now: datetime | None = None) -> str | None:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 5 Days Ago' / 'Posted 30+ Days Ago' -> ISO date."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    low = text.lower()
    if "today" in low:
        days = 0
    elif "yesterday" in low:
        days = 1
    else:
        m = _POSTED.search(low)
        if not m:
            return None
        days = int(m.group(1))
    return (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def _listing(slug: str, query: str = "") -> tuple[list[dict], bool]:
    """Postings (newest first) and whether the list is complete."""
    out: dict[str, dict] = {}
    total = None
    offset = 0
    while len(out) < MAX_POSTINGS:
        page = _page(slug, offset, query=query)
        if total is None:
            total = page.get("total", 0)   # only the first page carries the total
        posts = page.get("jobPostings") or []
        for p in posts:
            if p.get("externalPath"):
                out.setdefault(p["externalPath"], p)
        offset += len(posts)
        if not posts or offset >= (total or 0):
            break
    return list(out.values()), offset >= (total or 0)


def to_job(slug: str, name: str, p: dict) -> Job:
    tenant, _, site = slug.split("/")
    path = p["externalPath"]
    location = p.get("locationsText") or ""
    if _MANY_LOCATIONS.fullmatch(location):
        location = ""   # filled in from the details
    return Job(
        source=NAME,
        source_id=f"{tenant}/{site}/{path.rstrip('/').rsplit('/', 1)[-1]}",
        title=(p.get("title") or "").strip(),
        company=name,
        url=f"{_host(slug)}/{site}{path}",
        location=location,
        remote=looks_remote(location) or None,
        posted_at=posted_on(p.get("postedOn")),
        board=f"workday:{slug}",
    )


def fill_details(job: Job, d: dict) -> None:
    info = d.get("jobPostingInfo") or {}
    job.description = html_to_text(info.get("jobDescription"))
    locations = [info.get("location") or ""] + list(info.get("additionalLocations") or [])
    location = "; ".join(x for x in locations if x)
    if location:
        job.location = location
    remote_type = (info.get("remoteType") or "").lower()
    job.remote = ("remote" in remote_type) if remote_type else (looks_remote(job.location) or None)
    job.posted_at = to_iso(info.get("startDate")) or job.posted_at
    job.url = info.get("externalUrl") or job.url


def fetch_board(slug: str, name: str, ctx) -> BoardResult:
    # Searching for the title words skips most of a bank's or retailer's postings. Every job we
    # could store matches the search, so a complete result still tells us which stored jobs closed.
    postings, complete = _listing(slug, search_query(ctx.search))
    jobs = [to_job(slug, name, p) for p in postings]
    listed = [j.id for j in jobs]
    paths = {j.id: p["externalPath"] for j, p in zip(jobs, postings)}

    def detail(job: Job) -> Job | None:
        try:
            fill_details(job, get_json(f"{_api(slug)}{paths[job.id]}"))
        except Exception as e:   # retried on the next run
            log.debug("workday:%s detail failed for %s: %s", slug, job.id, e)
            return None
        return job

    with ThreadPoolExecutor(4) as ex:
        done = [j for j in ex.map(detail, [j for j in jobs if ctx.wants(j)]) if j and j.description]
    # A partial listing can't tell which stored jobs closed; the pipeline ages those out instead.
    return BoardResult(board=f"workday:{slug}", jobs=done, snapshot=complete, listed_ids=listed)
