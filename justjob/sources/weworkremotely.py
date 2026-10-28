"""We Work Remotely category RSS feeds: https://weworkremotely.com/remote-job-rss-feed"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from functools import partial

from ..models import BoardResult, Job
from ..net import request
from ..text import html_to_text

NAME = "weworkremotely"
SNAPSHOT = False
FEED = "https://weworkremotely.com/categories/{category}.rss"


def tasks(sc: dict):
    return [(f"weworkremotely:{c}", partial(fetch_category, c)) for c in sc.get("categories", [])]


def _date(value: str | None) -> str | None:
    try:
        return parsedate_to_datetime(value).isoformat(timespec="seconds") if value else None
    except (TypeError, ValueError):
        return None


def parse(xml: bytes) -> list[Job]:
    jobs = []
    for item in ET.fromstring(xml).iter("item"):
        link = (item.findtext("link") or "").strip()
        heading = (item.findtext("title") or "").strip()
        company, sep, title = heading.partition(": ")   # "Company: Job title"
        if not sep:
            company, title = "", heading
        if not link or not title:
            continue
        region = (item.findtext("region") or "").strip()
        country = (item.findtext("country") or "").strip()
        jobs.append(Job(
            source=NAME,
            source_id=link.rstrip("/").rsplit("/", 1)[-1],
            title=title,
            company=company,
            url=link,
            location="Remote - " + ", ".join(x for x in (region, country) if x) if region or country else "Remote",
            remote=True,
            description=html_to_text(item.findtext("description")),
            posted_at=_date(item.findtext("pubDate")),
            board="weworkremotely",
        ))
    return jobs


def fetch_category(category: str, ctx) -> BoardResult:
    return BoardResult(board="weworkremotely", jobs=parse(request(FEED.format(category=category))), snapshot=False)
