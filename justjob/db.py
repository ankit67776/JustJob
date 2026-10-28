"""SQLite storage. Every job ever fetched (that passed the title check) lives in `jobs`."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .models import Job
from .text import dedup_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    board         TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    location      TEXT NOT NULL DEFAULT '',
    remote        INTEGER,
    description   TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL,
    salary        TEXT NOT NULL DEFAULT '',
    posted_at     TEXT,
    dedup_key     TEXT NOT NULL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    closed        INTEGER NOT NULL DEFAULT 0,
    rule_status   TEXT,              -- pass | unclear | reject; NULL = not filtered yet
    rule_reason   TEXT,
    exp_years     REAL,              -- minimum years found by the regex
    rule_score    INTEGER,
    matched_skills TEXT,             -- JSON list of your skills found in the posting
    duplicate_of  TEXT,              -- id of the job this one duplicates
    ai_status     TEXT,              -- NULL = pending, 'done'
    ai_attempts   INTEGER NOT NULL DEFAULT 0,
    ai_min_years  REAL,
    ai_within     INTEGER,           -- 1/0: within max_years_required
    ai_score      INTEGER,
    ai_reason     TEXT,
    ai_flags      TEXT,              -- JSON list
    ai_model      TEXT,
    status        TEXT NOT NULL DEFAULT 'new',   -- new | applied | hidden
    status_at     TEXT
);
CREATE INDEX IF NOT EXISTS jobs_board ON jobs(board);
CREATE INDEX IF NOT EXISTS jobs_dedup ON jobs(dedup_key);
CREATE INDEX IF NOT EXISTS jobs_rule ON jobs(rule_status, closed);   -- the dashboard reads kept jobs only
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
-- One row per fetch task (a company board, feed or query): how its last fetches went.
CREATE TABLE IF NOT EXISTS boards (
    board       TEXT PRIMARY KEY,    -- e.g. "greenhouse:anthropic"
    checked_at  TEXT NOT NULL,
    ok_at       TEXT,
    listed      INTEGER NOT NULL DEFAULT 0,
    fails       INTEGER NOT NULL DEFAULT 0,   -- consecutive failed fetches
    error       TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    stats       TEXT
);
"""

def score_sql(cfg: dict) -> str:
    """The score jobs are ranked by: AI score if available, else the rule score, plus the
    remote bonus, minus a penalty for stale postings."""
    r = cfg.get("ranking", {})
    bonus, penalty = int(r.get("remote_bonus", 0)), int(r.get("stale_penalty", 0))
    stale_days = int(r.get("stale_after_days", 90))
    return (
        "MAX(0, MIN(100, COALESCE(ai_score, rule_score, 0)"
        f" + CASE WHEN remote = 1 THEN {bonus} ELSE 0 END"
        f" - CASE WHEN julianday('now') - julianday(COALESCE(posted_at, first_seen)) > {stale_days}"
        f" THEN {penalty} ELSE 0 END))"
    )


# A job worth showing: passed the rules, not a duplicate, still open, and the AI (if it ran)
# didn't say it needs too much experience.
SHOWABLE = (
    "rule_status IN ('pass', 'unclear') AND duplicate_of IS NULL AND closed = 0 "
    "AND COALESCE(ai_within, 1) = 1"
)
STATUSES = ("new", "applied", "hidden")


class DB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")   # safe with WAL; a fetch commits per board
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was created."""
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        for column, kind in (("matched_skills", "TEXT"),):
            if column not in have:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {kind}")

    def close(self) -> None:
        self.conn.close()

    def q(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    # -- meta -----------------------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # -- ingest ---------------------------------------------------------------------------

    def known_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT id FROM jobs")}

    def insert_jobs(self, jobs: list[Job], now: str) -> int:
        rows = [
            (
                j.id, j.source, j.board, j.title, j.company, j.location,
                None if j.remote is None else int(j.remote), j.description, j.url, j.salary,
                j.posted_at, dedup_key(j.company, j.title), now, now,
            )
            for j in jobs
        ]
        with self.conn:
            cur = self.conn.executemany(
                "INSERT OR IGNORE INTO jobs (id, source, board, title, company, location, remote,"
                " description, url, salary, posted_at, dedup_key, first_seen, last_seen)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return cur.rowcount

    def touch(self, ids: list[str], now: str) -> None:
        """Mark jobs as still listed (and reopen them if they had closed)."""
        with self.conn:
            self.conn.executemany(
                "UPDATE jobs SET last_seen = ?, closed = 0 WHERE id = ?", [(now, i) for i in ids]
            )

    def close_missing(self, board: str, listed: set[str]) -> int:
        open_ids = {r[0] for r in self.conn.execute(
            "SELECT id FROM jobs WHERE board = ? AND closed = 0", (board,))}
        gone = open_ids - listed
        if not listed and len(gone) > 5:
            return 0   # an empty board that used to have jobs is more likely an API hiccup
        with self.conn:
            self.conn.executemany("UPDATE jobs SET closed = 1 WHERE id = ?", [(i,) for i in gone])
        return len(gone)

    def close_unseen(self, board: str, before: str) -> int:
        """For boards fetched only partly: close their jobs not listed since `before`."""
        with self.conn:
            return self.conn.execute(
                "UPDATE jobs SET closed = 1 WHERE board = ? AND closed = 0 AND last_seen < ?", (board, before)
            ).rowcount

    def expire(self, sources: list[str], cutoff: str) -> int:
        if not sources:
            return 0
        marks = ",".join("?" * len(sources))
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE jobs SET closed = 1 WHERE closed = 0 AND source IN ({marks})"
                " AND COALESCE(posted_at, first_seen) < ?",
                (*sources, cutoff),
            )
        return cur.rowcount

    # -- board health ---------------------------------------------------------------------

    def boards_backing_off(self, now: datetime) -> set[str]:
        """Boards that failed 3+ times in a row and were tried recently. They're retried after
        1, 2, 4, ... days (at most 30), so a removed board stops costing a request every run."""
        out = set()
        for board, checked_at, fails in self.conn.execute(
                "SELECT board, checked_at, fails FROM boards WHERE fails >= 3"):
            wait = timedelta(days=min(2 ** (fails - 3), 30))
            if datetime.fromisoformat(checked_at) + wait > now:
                out.add(board)
        return out

    def board_checked_at(self) -> dict[str, str]:
        return dict(self.conn.execute("SELECT board, checked_at FROM boards").fetchall())

    def save_board_checks(self, checks: list[tuple[str, bool, int, str | None]], now: str) -> None:
        """checks: (board, ok, jobs listed, error)"""
        with self.conn:
            self.conn.executemany(
                "INSERT INTO boards (board, checked_at, ok_at, listed, fails, error) VALUES (?, ?, ?, ?, 0, NULL)"
                " ON CONFLICT(board) DO UPDATE SET checked_at = excluded.checked_at, ok_at = excluded.ok_at,"
                " listed = excluded.listed, fails = 0, error = NULL",
                [(b, now, now, n) for b, ok, n, _ in checks if ok],
            )
            self.conn.executemany(
                "INSERT INTO boards (board, checked_at, fails, error) VALUES (?, ?, 1, ?)"
                " ON CONFLICT(board) DO UPDATE SET checked_at = excluded.checked_at, fails = fails + 1,"
                " error = excluded.error",
                [(b, now, err) for b, ok, _, err in checks if not ok],
            )

    # -- rules / dedup / ai ---------------------------------------------------------------

    def save_rule_results(self, results: list[tuple]) -> None:
        """results: (status, reason, exp_years, score, matched_skills_json, id)"""
        with self.conn:
            self.conn.executemany(
                "UPDATE jobs SET rule_status = ?, rule_reason = ?, exp_years = ?, rule_score = ?,"
                " matched_skills = ? WHERE id = ?",
                results,
            )

    def save_ai_result(self, job_id: str, r: dict, model: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET ai_status = 'done', ai_min_years = ?, ai_within = ?, ai_score = ?,"
                " ai_reason = ?, ai_flags = ?, ai_model = ?, ai_attempts = ai_attempts + 1 WHERE id = ?",
                (
                    r["min_years_required"], int(r["is_within_limit"]), r["match_score"],
                    r["reason"], json.dumps(r["flags"]), model, job_id,
                ),
            )

    def bump_ai_attempts(self, ids: list[str]) -> None:
        with self.conn:
            self.conn.executemany(
                "UPDATE jobs SET ai_attempts = ai_attempts + 1 WHERE id = ?", [(i,) for i in ids]
            )

    # -- user actions ---------------------------------------------------------------------

    def set_status(self, job_id: str, status: str, now: str) -> bool:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        with self.conn:
            cur = self.conn.execute(
                "UPDATE jobs SET status = ?, status_at = ? WHERE id = ?", (status, now, job_id)
            )
        return cur.rowcount == 1
