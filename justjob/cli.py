"""Command line: python -m justjob <command>"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys

from . import ai, digest, discover, harvest, pipeline, registry, web
from .config import DATA_DIR, load_config
from .db import DB, score_sql, SHOWABLE, STATUSES
from .lock import run_lock
from .sources import ATS, SOURCES
from .text import now_iso

DB_PATH = DATA_DIR / "jobs.db"
LOCK_PATH = DATA_DIR / "run.lock"
WAIT_FOR_RUN = 45 * 60   # a full run waits this long for an hourly refresh (or another run) to finish


def setup_logging(verbose: bool) -> None:
    (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    logfile = logging.handlers.RotatingFileHandler(
        DATA_DIR / "logs" / "justjob.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    logfile.setLevel(logging.INFO)
    logfile.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(console)
    root.addHandler(logfile)


def _only(value: str | None) -> set[str] | None:
    if not value:
        return None
    names = {s.strip() for s in value.split(",") if s.strip()}
    unknown = names - set(SOURCES)
    if unknown:
        sys.exit(f"Unknown source(s): {', '.join(sorted(unknown))}. Known: {', '.join(SOURCES)}")
    return names


def _print_rows(rows) -> None:
    for r in rows:
        print(f"{r['score']:>3}  {r['title'][:60]:<60}  {r['company'][:22]:<22}  {web.experience_label(r):<11}  "
              f"{(r['location'] or '')[:28]}")
        posted = r["posted_at"] or r["first_seen"]
        print(f"     {web.why_label(r)[:90]}  [posted {posted[:10]}, {web.ago(posted)}]")
        print(f"     {r['url']}")
        print(f"     id: {r['id']}")


def cmd_run(cfg, db, args):
    # The hourly refresh just skips a turn if a run is going; anything else waits for it.
    with run_lock(LOCK_PATH, wait=0 if args.hot else WAIT_FOR_RUN) as got:
        if not got:
            print("Another run is in progress; skipping this refresh." if args.hot else
                  "Another run is still going after 45 minutes; not starting a second one.")
            return
        stats = pipeline.run(cfg, db, _only(args.only), use_ai=not args.no_ai, send=not args.no_notify, hot=args.hot)
    f, a, d = stats["fetch"], stats["ai"], stats.get("digest")
    print(f"\nFetched {f['listed']} listings from {f['tasks']} boards/queries: {f['new']} new stored, "
          f"{f['closed']} closed, {f['errors']} errors")
    print(f"Rules on new jobs: {stats['rules']}")
    print(f"Duplicates hidden: {stats['dedup']['duplicates']}")
    if "skipped" in a:
        print(f"AI: skipped ({a['skipped']})")
    else:
        print(f"AI: {a['calls']} calls, {a['analyzed']} analyzed, {a['failed']} failed, {a['pending']} still pending")
    if d:
        print(f"Digest: {d['new']} new matches, {d['open']} open -> {d['html']}")
    if stats.get("notify"):
        print(f"Notify: {stats['notify']}")
    if f["error_list"]:
        print("Fetch errors:\n  " + "\n  ".join(f["error_list"]))


def cmd_fetch(cfg, db, args):
    with run_lock(LOCK_PATH, wait=WAIT_FOR_RUN) as got:
        if not got:
            sys.exit("Another run is still going after 45 minutes.")
        stats = pipeline.fetch(cfg, db, _only(args.only))
    print(f"Fetched {stats['listed']} listings, {stats['new']} new, {stats['errors']} errors")
    print(f"Rules: {pipeline.apply_rules(cfg, db)}")
    print(f"Dedup: {pipeline.dedup(db)}")


def cmd_refilter(cfg, db, args):
    print(f"Rules: {pipeline.apply_rules(cfg, db, everything=True)}")
    print(f"Dedup: {pipeline.dedup(db)}")


def cmd_analyze(cfg, db, args):
    if args.redo:
        with db.conn:
            n = db.conn.execute(
                "UPDATE jobs SET ai_status = NULL, ai_attempts = 0, ai_min_years = NULL, ai_within = NULL,"
                " ai_score = NULL, ai_reason = NULL, ai_flags = NULL, ai_model = NULL"
                " WHERE closed = 0 AND status = 'new'").rowcount
        print(f"Cleared AI results for {n} open jobs")
    with run_lock(LOCK_PATH, wait=WAIT_FOR_RUN) as got:
        if not got:
            sys.exit("Another run is still going after 45 minutes.")
        print(f"AI: {ai.run(cfg, db)}")


def cmd_digest(cfg, db, args):
    d = digest.build(cfg, db)
    print(digest.write(cfg, d))


def cmd_list(cfg, db, args):
    score_expr = score_sql(cfg)
    min_score = 0 if args.all else int(cfg["digest"].get("min_score", 0))
    sql = f"SELECT *, {score_expr} AS score FROM jobs WHERE {SHOWABLE} AND status = 'new' AND {score_expr} >= ?"
    params: list = [min_score]
    if args.days:
        sql += f" AND julianday('now') - julianday({web.POSTED_AT}) <= ?"
        params.append(args.days)
    order = f"{web.POSTED_AT} DESC, score DESC" if args.newest else "score DESC"
    rows = db.q(f"{sql} ORDER BY {order} LIMIT ?", (*params, args.n))
    _print_rows(rows)
    if not rows:
        print("No matches yet. Run: python -m justjob run")


def cmd_mark(cfg, db, args):
    matches = db.q("SELECT id FROM jobs WHERE id = ? OR id LIKE ?", (args.id, f"%{args.id}"))
    if len(matches) != 1:
        sys.exit(f"{len(matches)} jobs match '{args.id}'; use the full id from `list`")
    db.set_status(matches[0]["id"], args.status, now_iso())
    print(f"{matches[0]['id']} -> {args.status}")


def cmd_stats(cfg, db, args):
    tracked = {ats: len(registry.load(ats)) for ats in ATS}
    print(f"Companies in companies/*.tsv: {sum(tracked.values())}  "
          + "  ".join(f"{a} {n}" for a, n in sorted(tracked.items(), key=lambda x: -x[1]) if n))
    b = db.q("SELECT COUNT(*) n, SUM(fails = 0) ok, SUM(fails > 0) failing, SUM(listed) listed FROM boards")[0]
    if b["n"]:
        print(f"Boards fetched: {b['n']}  ok {b['ok']}  failing {b['failing']}  jobs listed {b['listed']}")
    print("By source (open jobs):")
    for r in db.q("SELECT source, COUNT(*) n, SUM(rule_status = 'pass') p, SUM(rule_status = 'unclear') u,"
                  " SUM(rule_status = 'reject') x FROM jobs WHERE closed = 0 GROUP BY source ORDER BY n DESC"):
        print(f"  {r['source']:<15} {r['n']:>6} stored   pass {r['p'] or 0:>5}   unclear {r['u'] or 0:>5}   reject {r['x'] or 0:>6}")
    showable = db.q(f"SELECT COUNT(*) FROM jobs WHERE {SHOWABLE}")[0][0]
    by_status = dict(db.q(f"SELECT status, COUNT(*) FROM jobs WHERE {SHOWABLE} GROUP BY status"))
    ai_done = db.q(f"SELECT COUNT(*) FROM jobs WHERE {SHOWABLE} AND ai_status = 'done'")[0][0]
    print(f"Showable: {showable}  {by_status}  AI-analyzed: {ai_done}")
    print("Top rejection reasons:")
    for r in db.q("SELECT rule_reason, COUNT(*) n FROM jobs WHERE rule_status = 'reject' AND closed = 0"
                  " GROUP BY rule_reason ORDER BY n DESC LIMIT 10"):
        print(f"  {r['n']:>5}  {r['rule_reason']}")


def cmd_models(cfg, db, args):
    provs = ai.providers(cfg)
    if not provs:
        sys.exit("No AI provider has an API key set in .env")
    for p in provs:
        if args.provider and p.name != args.provider:
            continue
        print(f"{p.name} (configured model: {p.model}):")
        try:
            for m in ai.list_models(p):
                print(f"  {m}")
        except Exception as e:
            print(f"  failed: {e}")


def cmd_discover(cfg, db, args):
    candidates = discover.find_links(db, cfg)[: args.limit]
    if not candidates:
        print("No new company boards linked from stored jobs.")
        return
    print(f"Checking {len(candidates)} linked boards...")
    found = discover.verify(candidates)
    for c in found:
        print(f"  {c.ats:<16} {c.slug:<30} {c.jobs:>4} open jobs  {discover.display_name(c)}  (linked {c.mentions}x)")
    if not found:
        print("None of them have open jobs.")
    elif args.add:
        print(f"Added {discover.add_to_registry(found)} boards to companies/")
    else:
        print("Run again with --add to add them to companies/")


def cmd_harvest(cfg, db, args):
    only = _only(args.only)
    if only and only - set(ATS):
        sys.exit(f"Not a company-board source: {', '.join(sorted(only - set(ATS)))}. Known: {', '.join(ATS)}")
    crawls = harvest.recent_crawls(args.crawls)
    if not crawls:
        sys.exit("Couldn't reach Common Crawl (data.commoncrawl.org)")
    print(f"Reading the Common Crawl index for {', '.join(crawls)}...")
    found = harvest.collect(crawls, only)
    checked = harvest.load_checked()
    todo = harvest.candidates(cfg, found, args.recheck_days, checked)
    if args.limit:
        todo = {ats: slugs[: args.limit] for ats, slugs in todo.items()}
    for ats in sorted(found):
        print(f"  {ats:<16} {len(found[ats]):>6} companies seen, {len(todo.get(ats, [])):>6} new to check")
    total = sum(len(v) for v in todo.values())
    if not total:
        print("Nothing new to check.")
        return
    print(f"Checking {total} boards against their APIs (progress is saved as it goes)...")
    live = harvest.verify(cfg, todo, args.min_jobs, checked)
    for ats in sorted(live):
        print(f"  {ats:<16} +{len(live[ats])} live boards")
    print(f"Added {sum(len(v) for v in live.values())} boards to companies/. They're fetched from the next run on.")


def cmd_serve(cfg, db, args):
    if not web.is_loopback(args.host) and not os.environ.get("DASHBOARD_PASSWORD"):
        sys.exit(f"Set DASHBOARD_PASSWORD in .env before serving on {args.host}: "
                 "anyone who can reach it could read and change your list.")
    db.close()
    web.serve(cfg, str(DB_PATH), args.port, open_browser=not args.no_browser, host=args.host)


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        # Piped or scheduled output would otherwise use the legacy Windows code page.
        sys.stdout.reconfigure(errors="replace", **({} if sys.stdout.isatty() else {"encoding": "utf-8"}))
    ap = argparse.ArgumentParser(prog="python -m justjob", description="Collect, filter and rank job postings.")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="full daily pipeline: fetch, filter, dedup, AI, digest, notify")
    p.add_argument("--only", help="comma-separated sources, e.g. greenhouse,ashby")
    p.add_argument("--no-ai", action="store_true")
    p.add_argument("--no-notify", action="store_true")
    p.add_argument("--hot", action="store_true",
                   help="quick refresh: only companies with recent matches and the remote boards (no digest)")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("fetch", help="fetch + filter + dedup only")
    p.add_argument("--only")
    p.set_defaults(fn=cmd_fetch)

    sub.add_parser("refilter", help="re-apply filters to all stored jobs after editing config.toml").set_defaults(fn=cmd_refilter)

    p = sub.add_parser("analyze", help="run the AI step on pending jobs")
    p.add_argument("--redo", action="store_true", help="re-analyze open jobs (e.g. after changing your profile)")
    p.set_defaults(fn=cmd_analyze)

    sub.add_parser("digest", help="rewrite today's digest files").set_defaults(fn=cmd_digest)

    p = sub.add_parser("list", help="print top matches")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--all", action="store_true", help="ignore digest.min_score")
    p.add_argument("--days", type=int, help="only jobs posted in the last N days")
    p.add_argument("--newest", action="store_true", help="sort by posted date instead of score")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("mark", help="mark a job applied / hidden / new")
    p.add_argument("id", help="job id (or a unique ending of it)")
    p.add_argument("status", choices=STATUSES)
    p.set_defaults(fn=cmd_mark)

    sub.add_parser("stats", help="counts per source and top rejection reasons").set_defaults(fn=cmd_stats)

    p = sub.add_parser("models", help="list models your AI key can use")
    p.add_argument("--provider")
    p.set_defaults(fn=cmd_models)

    p = sub.add_parser("harvest", help="find thousands of company boards via Common Crawl and add the live ones")
    p.add_argument("--crawls", type=int, default=3, help="how many recent monthly crawls to read (more = more companies)")
    p.add_argument("--only", help="comma-separated ATS, e.g. greenhouse,workday")
    p.add_argument("--min-jobs", type=int, default=1, help="keep boards with at least this many open jobs")
    p.add_argument("--recheck-days", type=int, default=30, help="re-check boards found dead after this many days")
    p.add_argument("--limit", type=int, help="check at most this many new boards per ATS (for a quick try)")
    p.set_defaults(fn=cmd_harvest)

    p = sub.add_parser("discover", help="find company job boards linked from fetched jobs")
    p.add_argument("--add", action="store_true", help="add the boards found to companies/")
    p.add_argument("--limit", type=int, default=60, help="check at most this many links")
    p.set_defaults(fn=cmd_discover)

    p = sub.add_parser("serve", help="open the local dashboard")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1", help="address to listen on; anything but this computer "
                                                        "needs DASHBOARD_PASSWORD in .env")
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    cfg = load_config()
    db = DB(DB_PATH)
    try:
        args.fn(cfg, db, args)
    finally:
        db.close()
