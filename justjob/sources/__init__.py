"""Job sources. Each module exposes:

    NAME: str
    SNAPSHOT: bool    # True if a fetch returns the full current listing (closed jobs disappear)
    tasks(source_cfg: dict) -> list[tuple[label, fn(ctx) -> BoardResult]]

Per-company sources (applicant tracking systems) also expose, for `discover`:

    LINK: re.Pattern               # matches links to a company's board; group 1 is the slug
    probe(slug) -> (jobs, name)    # how many open jobs the board has, and the company name if known
    slug_from(match) -> str        # optional: build the slug from a LINK match ("" = not a board)

Any source may set CONCURRENCY, the number of its boards fetched at once (default: fetch.workers),
and MAX_BOARDS, a cap on boards per run for sites with a daily request budget.

Tasks run in a thread pool, so `fn` must not touch the database.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable

from .. import registry
from ..models import BoardResult, Job
from . import (adzuna, ashby, bamboohr, breezy, gem, greenhouse, hackernews, himalayas, jobicy, jsearch, lever,
               personio, recruitee, remoteok, remotive, rippling, smartrecruiters, teamtailor, weworkremotely,
               workable, workday, workingnomads)

log = logging.getLogger(__name__)

MODULES = (
    # company job boards (applicant tracking systems)
    greenhouse, lever, ashby, workday, smartrecruiters, workable, bamboohr, breezy, recruitee, personio,
    teamtailor, rippling, gem,
    # remote job boards
    remotive, remoteok, weworkremotely, himalayas, jobicy, workingnomads,
    # aggregators and forums
    jsearch, adzuna, hackernews,
)
# Sources whose boards are per company, so new companies can be found by `discover`.
ATS = {m.NAME: m for m in MODULES if hasattr(m, "probe")}
SOURCES = {m.NAME: m for m in MODULES}
DISPLAY = {
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "smartrecruiters": "SmartRecruiters",
    "remotive": "Remotive", "remoteok": "Remote OK", "hackernews": "HN Who's Hiring", "jsearch": "JSearch",
    "adzuna": "Adzuna", "workable": "Workable", "bamboohr": "BambooHR", "breezy": "Breezy HR",
    "recruitee": "Recruitee", "personio": "Personio", "weworkremotely": "We Work Remotely",
    "himalayas": "Himalayas", "jobicy": "Jobicy", "workingnomads": "Working Nomads", "workday": "Workday",
    "teamtailor": "Teamtailor", "rippling": "Rippling", "gem": "Gem",
}


def source_label(source: str, board: str) -> str:
    """'Greenhouse', or for aggregators the site the job came from: 'LinkedIn (JSearch)'."""
    if source == "jsearch" and ":" in board:
        return f"{board.split(':', 1)[1]} (JSearch)"
    return DISPLAY.get(source, source)


@dataclass
class FetchContext:
    # True if the job isn't stored yet and its title passes the title filter. Sources that need
    # an extra request per job for the description use this to skip ones we'd discard anyway.
    wants: Callable[[Job], bool]
    # Words every storable title contains one of (filters.title role words and standalone
    # phrases). Sources with a search (Workday) ask only for these instead of every posting.
    search: tuple[str, ...] = ()


Task = tuple[str, Callable[[FetchContext], BoardResult]]


def slug_of(ats: str, m: re.Match) -> str:
    mod = ATS[ats]
    return mod.slug_from(m) if hasattr(mod, "slug_from") else m.group(1).lower()


def with_registry(name: str, sc: dict) -> dict:
    """The source's config with the companies/<name>.tsv boards added to `boards`."""
    configured = {s.lower() for key, boards in sc.items() if key.endswith("boards") for s in boards}
    extra = {s: n for s, n in registry.boards(name).items() if s.lower() not in configured}
    return {**sc, "boards": {**extra, **sc.get("boards", {})}}


def concurrency(cfg: dict, name: str) -> int:
    sc = cfg["sources"].get(name, {})
    return int(sc.get("concurrency") or getattr(SOURCES.get(name), "CONCURRENCY", 0) or cfg["fetch"].get("workers", 6))


def build_tasks(cfg: dict, only: set[str] | None = None) -> list[Task]:
    tasks: list[Task] = []
    use_registry = cfg["fetch"].get("registry", True)
    for name, mod in SOURCES.items():
        sc = cfg["sources"].get(name, {})
        if only and name not in only:
            continue
        if not sc.get("enabled", False):
            continue
        if use_registry and name in ATS:
            sc = with_registry(name, sc)
        tasks.extend(mod.tasks(sc))
    return tasks


def non_snapshot_sources() -> list[str]:
    return [name for name, mod in SOURCES.items() if not mod.SNAPSHOT]
