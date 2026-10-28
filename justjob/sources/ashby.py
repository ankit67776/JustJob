"""Ashby public job board API: https://developers.ashbyhq.com/docs/public-job-posting-api"""
from __future__ import annotations

import re
from functools import partial

from ..models import BoardResult, Job
from ..net import get_json
from ..text import html_to_text, looks_remote, to_iso

NAME = "ashby"
SNAPSHOT = True
API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
LINK = re.compile(r"jobs\.ashbyhq\.com/([a-z0-9.-]+)", re.I)


def probe(slug: str) -> tuple[int, str]:
    return len(get_json(API.format(slug=slug)).get("jobs", [])), ""


def tasks(sc: dict):
    return [(f"ashby:{slug}", partial(fetch_board, slug, name)) for slug, name in sc.get("boards", {}).items()]


def fetch_board(slug: str, name: str, ctx) -> BoardResult:
    data = get_json(API.format(slug=slug), params={"includeCompensation": "true"})
    jobs = []
    for p in data.get("jobs", []):
        if p.get("isListed") is False:
            continue
        locations = [p.get("location") or ""]
        locations += [s.get("location", "") for s in p.get("secondaryLocations") or []]
        location = "; ".join(x for x in locations if x)
        # isRemote is also true for hybrid roles, so prefer workplaceType when it's set.
        workplace = (p.get("workplaceType") or "").lower()
        if workplace:
            remote = workplace == "remote"
        else:
            remote = p.get("isRemote") if p.get("isRemote") is not None else (looks_remote(location) or None)
        comp = p.get("compensation") or {}
        jobs.append(Job(
            source=NAME,
            source_id=f"{slug}/{p['id']}",
            title=(p.get("title") or "").strip(),
            company=name,
            url=p.get("jobUrl") or p.get("applyUrl") or "",
            location=location,
            remote=remote,
            description=p.get("descriptionPlain") or html_to_text(p.get("descriptionHtml")),
            posted_at=to_iso(p.get("publishedAt")),
            salary=comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary") or "",
            board=f"ashby:{slug}",
        ))
    return BoardResult(board=f"ashby:{slug}", jobs=jobs, snapshot=True)
