"""The daily pipeline: fetch -> store new -> rule filter -> dedup -> AI -> digest -> notify."""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from . import ai, digest, notify
from .db import DB
from .filters import Rules
from .net import HttpError
from .sources import ATS, SOURCES, FetchContext, build_tasks, concurrency, non_snapshot_sources
from .text import now_iso

log = logging.getLogger(__name__)

SKIPPED = object()   # what a board returns when its source was rate-limiting us earlier in the run
# Job APIs with small monthly quotas: queried on full runs only, never by the hourly refresh.
QUOTA_SOURCES = {"jsearch", "adzuna"}

# Which copy of a duplicated job to keep: direct company boards first, aggregators last.
SOURCE_RANK = {"greenhouse": 0, "lever": 0, "ashby": 0, "smartrecruiters": 0, "workable": 0, "bamboohr": 0,
               "recruitee": 0, "personio": 0, "workday": 0, "teamtailor": 0, "rippling": 0, "gem": 0,
               "breezy": 1, "jsearch": 1, "adzuna": 2, "remotive": 2, "remoteok": 2, "weworkremotely": 2,
               "himalayas": 2, "jobicy": 2, "workingnomads": 2, "hackernews": 3}


def hot_tasks(cfg: dict, db: DB, tasks: list) -> list:
    """The hourly refresh: company boards that produced a match in the last `fetch.hot_days`,
    boards picked by hand in config.toml, and the remote job boards (a request or two each).

    Sources with a daily request budget (Workable's `max_boards`) are left to the full runs:
    hourly visits on top would use the budget up and get us blocked for a day."""
    since = datetime.now(timezone.utc) - timedelta(days=int(cfg["fetch"].get("hot_days", 60)))
    active = db.active_boards(since.isoformat(timespec="seconds"))
    picked = {f"{ats}:{slug}" for ats in ATS for key, boards in cfg["sources"].get(ats, {}).items()
              if key.endswith("boards") and isinstance(boards, dict) for slug in boards}
    out = []
    for label, fn in tasks:
        source = label.split(":", 1)[0]
        budgeted = cfg["sources"].get(source, {}).get("max_boards") or getattr(SOURCES.get(source), "MAX_BOARDS", 0)
        if budgeted:
            continue
        if (label in active or label in picked) if source in ATS else source not in QUOTA_SOURCES:
            out.append((label, fn))
    return out


def fetch(cfg: dict, db: DB, only: set[str] | None = None, hot: bool = False) -> dict:
    rules = Rules(cfg)
    known = db.known_ids()
    title_ok = lambda title: rules.title_ok(title)[0]   # noqa: E731
    title_cfg = cfg["filters"]["title"]
    search = tuple(dict.fromkeys(title_cfg.get("role_words", []) + title_cfg.get("standalone", [])))
    ctx = FetchContext(wants=lambda job: job.id not in known and title_ok(job.title), search=search)
    now = now_iso()
    tasks = build_tasks(cfg, only)
    if hot:
        tasks = hot_tasks(cfg, db, tasks)
    resting = db.boards_backing_off(datetime.fromisoformat(now))
    skipped = sum(1 for label, _ in tasks if label in resting)
    tasks = rotate(cfg, db, [t for t in tasks if t[0] not in resting])
    unseen_before = (datetime.fromisoformat(now) - timedelta(days=int(cfg["fetch"].get("unseen_days", 7))))
    stats = Counter(listed=0, new=0, closed=0, errors=0, rate_limited=0)
    per_source: dict[str, Counter] = defaultdict(Counter)
    errors: list[str] = []
    checks: list[tuple[str, bool, int, str | None]] = []
    blocked: set[str] = set()

    # Each source gets its own pool, so a slow or rate-limited site (Workable) never holds up the
    # thousands of boards on the others. Results are stored here, on the main thread.
    pools: dict[str, ThreadPoolExecutor] = {}
    futures = {}

    def unless_blocked(source: str, fn):
        # Boards of a source that's rate-limiting us finish at once without a request. (Cancelling
        # their futures instead would hang as_completed(): cancelled futures never complete.)
        return lambda c: SKIPPED if source in blocked else fn(c)

    try:
        for label, fn in tasks:
            source = label.split(":", 1)[0]
            if source not in pools:
                pools[source] = ThreadPoolExecutor(concurrency(cfg, source), thread_name_prefix=source)
            futures[pools[source].submit(unless_blocked(source, fn), ctx)] = label
        for done, fut in enumerate(as_completed(futures), 1):
            label = futures[fut]
            source = label.split(":", 1)[0]
            if done % 1000 == 0:
                log.info("fetch: %d/%d boards done, %d new jobs so far", done, len(futures), stats["new"])
            try:
                res = fut.result()
            except Exception as e:
                if isinstance(e, HttpError) and e.status == 429:
                    # The site's limit, not the board's fault. One busy Workday tenant says little,
                    # but a long block (Workable's ~1 day) or a run of 429s means stop asking this
                    # source for the rest of the run instead of collecting thousands of errors.
                    stats["rate_limited"] += 1
                    per_source[source]["rate_limited"] += 1
                    if source not in blocked and ((e.retry_after or 0) > 600 or per_source[source]["rate_limited"] >= 25):
                        blocked.add(source)
                        hours = f" for {e.retry_after / 3600:.0f}h" if (e.retry_after or 0) > 3600 else ""
                        log.warning("fetch: %s is rate-limiting us%s; its remaining boards wait for the next run",
                                    source, hours)
                    continue
                log.debug("fetch %s failed: %s", label, e)
                errors.append(f"{label}: {e}")
                checks.append((label, False, 0, str(e)[:300]))
                per_source[source]["errors"] += 1
                stats["errors"] += 1
                continue
            if res is SKIPPED:   # its source is rate-limiting us; tried again next run
                stats["rate_limited"] += 1
                continue
            listed = set(res.listed_ids if res.listed_ids is not None else [j.id for j in res.jobs])
            checks.append((label, True, len(listed), None))
            new = []
            for job in res.jobs:
                if job.id in known or not job.url or not job.title:
                    continue
                if not title_ok(job.title):
                    continue
                new.append(job)
                known.add(job.id)
            db.touch([i for i in listed if i in known], now)
            inserted = db.insert_jobs(new, now)
            if res.snapshot:
                closed = db.close_missing(res.board, listed)
            elif getattr(SOURCES.get(source), "SNAPSHOT", False):
                # A company board that listed only its newest jobs: close what hasn't shown up lately.
                closed = db.close_unseen(res.board, unseen_before.isoformat(timespec="seconds"))
            else:
                closed = 0
            per_source[source]["listed"] += len(listed)
            per_source[source]["new"] += inserted
            per_source[source]["closed"] += closed
            stats["listed"] += len(listed)
            stats["new"] += inserted
            stats["closed"] += closed
    finally:
        for pool in pools.values():
            pool.shutdown(wait=False, cancel_futures=True)
        db.save_board_checks(checks, now)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(cfg["fetch"].get("max_age_days", 30))))
    stats["expired"] = db.expire(non_snapshot_sources(), cutoff.isoformat(timespec="seconds"))
    failing = {s: c["errors"] for s, c in per_source.items() if c["errors"]}
    log.info("fetch: %d tasks (%d failing boards resting), %d listed, %d new, %d closed, %d errors %s%s",
             len(tasks), skipped, stats["listed"], stats["new"], stats["closed"], stats["errors"], failing or "",
             f", {stats['rate_limited']} rate-limited" if stats["rate_limited"] else "")
    return {**stats, "tasks": len(tasks), "skipped": skipped, "sources": {k: dict(v) for k, v in per_source.items()},
            "error_list": errors[:20]}


def rotate(cfg: dict, db: DB, tasks: list) -> list:
    """Cap boards per run for sources with a daily request budget (`max_boards`, e.g. Workable),
    taking the ones checked longest ago, so every board still comes round every few runs."""
    checked = None
    by_source: dict[str, list] = defaultdict(list)
    for t in tasks:
        by_source[t[0].split(":", 1)[0]].append(t)
    out = []
    for source, items in by_source.items():
        cap = int(cfg["sources"].get(source, {}).get("max_boards") or getattr(SOURCES.get(source), "MAX_BOARDS", 0))
        if cap and len(items) > cap:
            checked = checked if checked is not None else db.board_checked_at()
            items = sorted(items, key=lambda t: checked.get(t[0], ""))[:cap]
        out.extend(items)
    return out


def apply_rules(cfg: dict, db: DB, everything: bool = False) -> dict:
    """Run the rule filter on unfiltered jobs (or all open ones, after you change the config)."""
    rules = Rules(cfg)
    where = "closed = 0" if everything else "rule_status IS NULL"
    rows = db.q(f"SELECT id, title, location, remote, description FROM jobs WHERE {where}")
    results = []
    counts = Counter()
    for r in rows:
        remote = None if r["remote"] is None else bool(r["remote"])
        res = rules.classify(r["title"], r["location"], remote, r["description"])
        results.append((res.status, res.reason, res.exp_years, res.score, json.dumps(res.skills), r["id"]))
        counts[res.status] += 1
    db.save_rule_results(results)
    log.info("rules: %d jobs -> %s", len(rows), dict(counts))
    return dict(counts)


def dedup(db: DB) -> dict:
    """Group kept jobs by company + normalized title and keep one per group.

    Rejected jobs are left out on purpose: the same role posted in a city you can't work in
    mustn't hide the copy posted in one you can.
    """
    rows = db.q(
        "SELECT id, source, dedup_key, status, closed, first_seen, ai_status FROM jobs"
        " WHERE rule_status IN ('pass', 'unclear')"
    )
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[r["dedup_key"]].append(r)

    updates, copies = [], []
    for members in groups.values():
        # A job you already acted on stays canonical, so its duplicates stay hidden.
        members.sort(key=lambda r: (r["status"] == "new", r["closed"], SOURCE_RANK.get(r["source"], 9),
                                    r["first_seen"], r["id"]))
        keep = members[0]
        updates.append((None, keep["id"]))
        updates.extend((keep["id"], m["id"]) for m in members[1:])
        if keep["ai_status"] is None:
            donor = next((m for m in members[1:] if m["ai_status"] == "done"), None)
            if donor:
                copies.append((donor["id"], keep["id"]))

    with db.conn:
        db.conn.executemany("UPDATE jobs SET duplicate_of = ? WHERE id = ?", updates)
        for donor, target in copies:
            db.conn.execute(
                "UPDATE jobs SET (ai_status, ai_min_years, ai_within, ai_score, ai_reason, ai_flags, ai_model)"
                " = (SELECT ai_status, ai_min_years, ai_within, ai_score, ai_reason, ai_flags, ai_model"
                " FROM jobs WHERE id = ?) WHERE id = ?",
                (donor, target),
            )
    dupes = sum(1 for u in updates if u[0] is not None)
    log.info("dedup: %d kept jobs, %d duplicates", len(updates) - dupes, dupes)
    return {"groups": len(groups), "duplicates": dupes}


def run(cfg: dict, db: DB, only: set[str] | None = None, use_ai: bool = True, send: bool = True,
        hot: bool = False) -> dict:
    started = now_iso()
    with db.conn:
        run_id = db.conn.execute("INSERT INTO runs (started_at) VALUES (?)", (started,)).lastrowid

    stats: dict = {"hot": hot, "fetch": fetch(cfg, db, only, hot)}
    stats["rules"] = apply_rules(cfg, db)
    stats["dedup"] = dedup(db)
    stats["ai"] = ai.run(cfg, db) if use_ai else {"skipped": "--no-ai"}
    # The digest and notifications come with the full runs only, so "new since the last digest"
    # (and the dashboard's New tag) covers a few hours, not just the last hourly refresh.
    if not hot:
        d = digest.build(cfg, db)
        stats["digest"] = digest.write(cfg, d)
        if send:
            stats["notify"] = notify.send(cfg, d)
        db.set_meta("previous_digest_at", d.since or "")   # the dashboard tags jobs newer than this
        db.set_meta("last_digest_at", d.generated_at)

    with db.conn:
        db.conn.execute("UPDATE runs SET finished_at = ?, stats = ? WHERE id = ?",
                        (now_iso(), json.dumps(stats, default=str), run_id))
    return stats
