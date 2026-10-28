"""Find company job boards in bulk from Common Crawl's public URL index.

Covering tens of thousands of companies (the way Remote Rocketship does) needs their board
slugs, and applicant tracking systems don't list their customers. Common Crawl publishes an
index of every URL it crawls each month, sorted by host. For each ATS this downloads only the
slices of that index under its domain (a few MB per crawl), pulls the company slug out of every
URL, checks each new slug against the ATS's API, and saves the live ones to companies/<ats>.tsv.

Slugs that turned out dead or empty are remembered in data/harvest_checked.tsv and only
checked again after `recheck_days`.
"""
from __future__ import annotations

import gzip
import json
import logging
import re
import time
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import registry
from .config import DATA_DIR
from .net import RETRY_STATUS, USER_AGENT, HttpError, get_json
from .sources import ATS, concurrency, slug_of

log = logging.getLogger(__name__)

INDEX = "https://data.commoncrawl.org/cc-index/collections/{crawl}/indexes/"
COLLINFO = "https://index.commoncrawl.org/collinfo.json"
# Host prefixes in the index's SURT form (reversed host names), per ATS.
PREFIXES = {
    "greenhouse": ("io,greenhouse,",),
    "lever": ("co,lever,",),
    "ashby": ("com,ashbyhq,",),
    "workday": ("com,myworkdayjobs,",),
    "smartrecruiters": ("com,smartrecruiters,",),
    "workable": ("com,workable,",),
    "bamboohr": ("com,bamboohr,",),
    "breezy": ("hr,breezy,",),
    "recruitee": ("com,recruitee,",),
    "personio": ("de,personio,", "com,personio,"),
    "teamtailor": ("com,teamtailor,",),
    "rippling": ("com,rippling,",),
    "gem": ("com,gem,",),
}
# Path segments and subdomains that look like slugs but aren't companies.
NOT_SLUGS = {"www", "api", "app", "jobs", "careers", "embed", "boards", "v1", "o", "p", "j", "job", "apply",
             "help", "support", "blog", "status", "docs", "marketplace", "login", "signin", "static", "assets",
             "cdn", "images", "en", "de", "fr", "es", "robots.txt", "sitemap.xml", "favicon.ico"}
CHECKED = DATA_DIR / "harvest_checked.tsv"
_SLUG = re.compile(r"[a-z0-9][a-z0-9._-]*(?:/[a-z0-9._-]+)*")


def _get(url: str, headers: dict | None = None, timeout: float = 120, retries: int = 3) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:
            if attempt == retries:
                raise
            log.debug("harvest: %s failed (%s), retrying", url, e)
            time.sleep(2 ** (attempt + 1))
    raise AssertionError("unreachable")


def recent_crawls(n: int) -> list[str]:
    """IDs of the newest `n` crawls, e.g. CC-MAIN-2026-43."""
    try:
        return [c["id"] for c in get_json(COLLINFO, retries=1, timeout=30)][:n]
    except Exception as e:   # the index server is often overloaded; the data host isn't
        log.info("harvest: crawl list unavailable (%s); probing recent weeks instead", e)
    found: list[str] = []
    today = datetime.now(timezone.utc).date()
    for weeks_back in range(0, 80):
        year, week, _ = (today - timedelta(weeks=weeks_back)).isocalendar()
        crawl = f"CC-MAIN-{year}-{week:02d}"
        try:
            req = urllib.request.Request(INDEX.format(crawl=crawl) + "cluster.idx", method="HEAD",
                                         headers={"User-Agent": USER_AGENT})
            urllib.request.urlopen(req, timeout=20).close()
        except Exception:
            continue
        found.append(crawl)
        if len(found) == n:
            break
    return found


def index_slices(crawl: str, wanted: dict[str, tuple[str, ...]]) -> dict[str, list[tuple[str, int, int]]]:
    """(file, offset, length) of the compressed index blocks that cover each ATS's hosts.

    cluster.idx lists the first key of every block. A block can start just before a prefix and
    still contain matches, so the block before the first matching key is included too.
    """
    out: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    prev: tuple[str, int, int] | None = None
    prev_hits: set[str] = set()
    req = urllib.request.Request(INDEX.format(crawl=crawl) + "cluster.idx", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw in resp:
            key, _, rest = raw.decode("utf-8", "replace").partition(" ")
            fields = rest.rstrip("\n").split("\t")
            if len(fields) < 4:
                continue
            block = (fields[1], int(fields[2]), int(fields[3]))
            hits = {ats for ats, prefixes in wanted.items() if key.startswith(prefixes)}
            for ats in hits:
                if ats not in prev_hits and prev and (not out[ats] or out[ats][-1] != prev):
                    out[ats].append(prev)
                out[ats].append(block)
            prev, prev_hits = block, hits
    return dict(out)


def read_slice(crawl: str, block: tuple[str, int, int]) -> list[str]:
    """URLs in one compressed index block."""
    name, offset, length = block
    data = _get(INDEX.format(crawl=crawl) + name, headers={"Range": f"bytes={offset}-{offset + length - 1}"})
    urls = []
    for line in gzip.decompress(data).decode("utf-8", "replace").splitlines():
        _, _, rest = line.partition(" ")
        _, _, meta = rest.partition(" ")
        try:
            urls.append(json.loads(meta)["url"])
        except (ValueError, KeyError):
            continue
    return urls


def slugs_in(ats: str, urls: list[str]) -> Counter:
    counts: Counter = Counter()
    link = ATS[ats].LINK
    for url in urls:
        m = link.search(url)
        if not m:
            continue
        slug = slug_of(ats, m).strip(".-")
        if slug and slug.split("/")[0] not in NOT_SLUGS and _SLUG.fullmatch(slug):
            counts[slug] += 1
    return counts


def collect(crawls: list[str], only: set[str] | None = None) -> dict[str, Counter]:
    """Slug -> number of URLs seen, per ATS, over the given crawls."""
    wanted = {a: p for a, p in PREFIXES.items() if a in ATS and (not only or a in only)}
    found: dict[str, Counter] = defaultdict(Counter)
    for crawl in crawls:
        started = time.time()
        slices = index_slices(crawl, wanted)
        jobs = [(ats, block) for ats, blocks in slices.items() for block in blocks]
        with ThreadPoolExecutor(8) as ex:
            futures = {ex.submit(read_slice, crawl, block): ats for ats, block in jobs}
            for fut in as_completed(futures):
                try:
                    found[futures[fut]].update(slugs_in(futures[fut], fut.result()))
                except Exception as e:
                    log.warning("harvest: %s index block failed: %s", crawl, e)
        log.info("harvest: %s: %d index blocks in %.0fs; %s", crawl, len(jobs), time.time() - started,
                 ", ".join(f"{a} {len(c)}" for a, c in sorted(found.items())))
    return dict(found)


# -- checking candidates ------------------------------------------------------------------

def load_checked(path: Path = CHECKED) -> dict[tuple[str, str], str]:
    """(ats, slug) -> date last found dead or empty."""
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            out[(parts[0], parts[1])] = parts[2]
    return out


def save_checked(checked: dict[tuple[str, str], str], path: Path = CHECKED) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(f"{a}\t{s}\t{d}\n" for (a, s), d in sorted(checked.items())), encoding="utf-8")
    tmp.replace(path)


def candidates(cfg: dict, found: dict[str, Counter], recheck_days: int,
               checked: dict[tuple[str, str], str]) -> dict[str, list[str]]:
    """New slugs worth checking, most-linked first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=recheck_days)).date().isoformat()
    out = {}
    for ats, counts in found.items():
        sc = cfg["sources"].get(ats, {})
        known = {s.lower() for s in registry.load(ats)}
        known |= {s.lower() for key, boards in sc.items() if key.endswith("boards") for s in boards}
        out[ats] = [s for s, _ in counts.most_common()
                    if s not in known and checked.get((ats, s), "") < cutoff]
    return out


def verify(cfg: dict, todo: dict[str, list[str]], min_jobs: int = 1, checked=None,
           save_every: int = 200) -> dict[str, dict[str, tuple[str, int]]]:
    """Probe every candidate, each ATS in its own pool. Live boards go to companies/<ats>.tsv as
    they're found (so an interrupted run keeps its progress); dead ones to `checked`."""
    checked = {} if checked is None else checked
    today = datetime.now(timezone.utc).date().isoformat()
    live: dict[str, dict[str, tuple[str, int]]] = defaultdict(dict)
    pending: dict[str, dict[str, tuple[str, int]]] = defaultdict(dict)
    total = sum(len(v) for v in todo.values())
    done = retry = 0

    def check(ats: str, slug: str):
        try:
            jobs, name = ATS[ats].probe(slug)
        except HttpError as e:
            log.debug("harvest: %s:%s not usable: %s", ats, slug, e)
            # Rate limits and server errors say nothing about the board; try it next harvest.
            return ats, slug, None if e.status in RETRY_STATUS else 0, ""
        except Exception as e:   # timeouts, bad JSON from a non-board page, ...
            log.debug("harvest: %s:%s not usable: %s", ats, slug, e)
            return ats, slug, None if isinstance(e, (ConnectionError, TimeoutError)) else 0, ""
        return ats, slug, jobs, name

    def flush():
        for ats, rows in pending.items():
            if rows:
                registry.add(ats, rows)
        pending.clear()
        save_checked(checked)

    pools = {ats: ThreadPoolExecutor(concurrency(cfg, ats)) for ats in todo if todo[ats]}
    try:
        futures = [pools[ats].submit(check, ats, slug) for ats, slugs in todo.items() for slug in slugs]
        for fut in as_completed(futures):
            ats, slug, jobs, name = fut.result()
            done += 1
            if jobs is None:
                retry += 1
            elif jobs >= min_jobs and jobs > 0:
                row = (name.strip() or slug.split("/")[0].replace("-", " ").title(), jobs)
                live[ats][slug] = pending[ats][slug] = row
            else:
                checked[(ats, slug)] = today
            if done % save_every == 0 or done == total:
                flush()
                log.info("harvest: checked %d/%d, %d live boards so far, %d to retry next time", done, total,
                         sum(len(v) for v in live.values()), retry)
    finally:
        for pool in pools.values():
            pool.shutdown(wait=False, cancel_futures=True)
        flush()
    return dict(live)
