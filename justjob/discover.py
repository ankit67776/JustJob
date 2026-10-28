"""Find company job boards worth adding, from links inside jobs we've already fetched.

Applicant tracking systems (Greenhouse, Workable, BambooHR, ...) have no cross-company search,
so every company has to be listed (in companies/<ats>.tsv or config.toml). `harvest` finds most
of them in bulk; this catches the rest: job posts on HN, JSearch results and descriptions often
link to a company's board, so it collects those links, checks each board is live, and can add it
to companies/<ats>.tsv.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import registry
from .db import DB
from .harvest import NOT_SLUGS
from .sources import ATS, slug_of

log = logging.getLogger(__name__)

# Cheap substring check before running each source's link regex over every description.
DOMAINS = {"greenhouse": "greenhouse.io", "lever": "lever.co", "ashby": "ashbyhq.com",
           "smartrecruiters": "smartrecruiters.com", "workable": "workable.com", "bamboohr": "bamboohr.com",
           "breezy": "breezy.hr", "recruitee": "recruitee.com", "personio": "personio.",
           "workday": "myworkdayjobs.com", "teamtailor": "teamtailor.com", "rippling": "rippling.com",
           "gem": "jobs.gem.com"}


@dataclass
class Candidate:
    ats: str
    slug: str
    mentions: int
    jobs: int = 0
    name: str = ""


def configured(cfg: dict) -> dict[str, set[str]]:
    out = {}
    for ats in ATS:
        sc = cfg["sources"].get(ats, {})
        out[ats] = {s.lower() for key in ("boards", "eu_boards") for s in sc.get(key, {})}
        out[ats] |= {s.lower() for s in registry.load(ats)}
    return out


def find_links(db: DB, cfg: dict) -> list[Candidate]:
    known = configured(cfg)
    counts: dict[str, Counter] = {ats: Counter() for ats in ATS}
    for url, description in db.conn.execute("SELECT url, description FROM jobs"):
        text = f"{url}\n{description}"
        low = text.lower()
        for ats, mod in ATS.items():
            if DOMAINS.get(ats, "") not in low:
                continue
            for m in mod.LINK.finditer(text):
                slug = slug_of(ats, m).strip(".-")
                if (slug and slug.split("/")[0] not in NOT_SLUGS and slug not in known[ats]
                        and re.fullmatch(r"[a-z0-9][a-z0-9._-]*(?:/[a-z0-9._-]+)*", slug)):
                    counts[ats][slug] += 1
    return [Candidate(ats, slug, n) for ats, c in counts.items() for slug, n in c.most_common()]


def verify(candidates: list[Candidate]) -> list[Candidate]:
    """Keep boards that respond and have open jobs."""
    def check(c: Candidate) -> Candidate | None:
        try:
            c.jobs, c.name = ATS[c.ats].probe(c.slug)
        except Exception as e:
            log.debug("discover: %s:%s not usable: %s", c.ats, c.slug, e)
            return None
        return c if c.jobs else None

    with ThreadPoolExecutor(8) as ex:
        return [c for c in ex.map(check, candidates) if c]


def display_name(c: Candidate) -> str:
    return c.name.strip() or c.slug.replace("-", " ").title()


def add_to_registry(found: list[Candidate], base=None) -> int:
    """Save the boards to companies/<ats>.tsv; returns how many were new."""
    added = 0
    for ats in {c.ats for c in found}:
        added += registry.add(ats, {c.slug: (display_name(c), c.jobs) for c in found if c.ats == ats}, base)
    return added
