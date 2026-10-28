"""BambooHR public careers pages (<slug>.bamboohr.com/careers)."""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from ..models import BoardResult, Job
from ..net import get_json
from ..text import html_to_text, to_iso

NAME = "bamboohr"
SNAPSHOT = True
BASE = "https://{slug}.bamboohr.com/careers"
LINK = re.compile(r"(?<![a-z0-9-])([a-z0-9-]+)\.bamboohr\.com", re.I)
REMOTE_TYPE = "1"   # locationType: 0 on-site, 1 remote, 2 hybrid

log = logging.getLogger(__name__)


def tasks(sc: dict):
    return [(f"bamboohr:{slug}", partial(fetch_board, slug, name)) for slug, name in sc.get("boards", {}).items()]


def probe(slug: str) -> tuple[int, str]:
    return len(get_json(f"{BASE.format(slug=slug)}/list").get("result", [])), ""


def _location(p: dict) -> str:
    loc = p.get("atsLocation") or {}
    if not any(loc.values()):
        loc = p.get("location") or {}
    return ", ".join(x for x in (loc.get("city"), loc.get("state") or loc.get("province"), loc.get("country")) if x)


def fetch_board(slug: str, name: str, ctx) -> BoardResult:
    base = BASE.format(slug=slug)
    jobs = []
    for p in get_json(f"{base}/list").get("result", []):
        jobs.append(Job(
            source=NAME,
            source_id=f"{slug}/{p['id']}",
            title=(p.get("jobOpeningName") or "").strip(),
            company=name,
            url=f"{base}/{p['id']}",
            location=_location(p),
            remote=bool(p.get("isRemote")) or str(p.get("locationType")) == REMOTE_TYPE or None,
            board=f"bamboohr:{slug}",
        ))
    listed = [j.id for j in jobs]

    def detail(job: Job) -> Job | None:
        try:
            d = get_json(f"{base}/{job.source_id.split('/', 1)[1]}/detail").get("result", {}).get("jobOpening", {})
        except Exception as e:   # retried on the next run
            log.debug("bamboohr:%s detail failed for %s: %s", slug, job.id, e)
            return None
        text = html_to_text(d.get("description"))
        if d.get("minimumExperience"):
            text = f"Minimum experience: {d['minimumExperience']}\n\n{text}"
        job.description = text
        job.posted_at = to_iso(d.get("datePosted"))
        job.url = d.get("jobOpeningShareUrl") or job.url
        return job

    with ThreadPoolExecutor(4) as ex:
        done = [j for j in ex.map(detail, [j for j in jobs if ctx.wants(j)]) if j and j.description]
    return BoardResult(board=f"bamboohr:{slug}", jobs=done, snapshot=True, listed_ids=listed)
