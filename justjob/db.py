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
-- Your profile as one JSON document (see profile.py); a single row.
CREATE TABLE IF NOT EXISTS profile (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    data        TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
-- One row per job the apply agent worked on (see apply/).
CREATE TABLE IF NOT EXISTS applications (
    job_id       TEXT PRIMARY KEY,
    ats          TEXT NOT NULL,
    apply_url    TEXT NOT NULL,
    state        TEXT NOT NULL,     -- drafted | filling | waiting | submitted | closed | failed
    questions    TEXT NOT NULL,     -- JSON: the form's questions, in one schema for every ATS
    answers      TEXT NOT NULL,     -- JSON: question id -> answer
    report       TEXT,              -- JSON: what the browser step filled and what it left to you
    error        TEXT,
    screenshot   TEXT,              -- file name in data/applications/ of the confirmation page
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    submitted_at TEXT
);
-- The tracker's history: each stage change and note on a job you applied to (tracker.py).
CREATE TABLE IF NOT EXISTS track_events (
    id          INTEGER PRIMARY KEY,
    job_id      TEXT NOT NULL,
    kind        TEXT NOT NULL,      -- stage | note
    stage       TEXT,               -- the stage it moved to (kind = stage)
    note        TEXT NOT NULL DEFAULT '',
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS track_events_job ON track_events (job_id, at);
-- Answers you gave on submitted forms, reused when another form asks the same question (apply/memory.py).
CREATE TABLE IF NOT EXISTS answer_memory (
    key         TEXT PRIMARY KEY,   -- normalized question label + its options
    label       TEXT NOT NULL,
    value       TEXT NOT NULL,      -- JSON
    uses        INTEGER NOT NULL DEFAULT 0,   -- times it was saved
    updated_at  TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'text', -- the question's kind (see apply/forms.py)
    options     TEXT,               -- JSON: the question's options
    confirmed   INTEGER NOT NULL DEFAULT 1,   -- forms you gave or kept this exact answer on
    overrides   INTEGER NOT NULL DEFAULT 0,   -- 1 = you corrected the profile's or a rule's answer
    company     TEXT NOT NULL DEFAULT '',     -- the company whose form it was last given on
    job_id      TEXT NOT NULL DEFAULT ''
);
"""
# Columns added after a table was first created: (table, column, definition).
ADDED_COLUMNS = (
    ("jobs", "matched_skills", "TEXT"),
    ("jobs", "stage", "TEXT"),          # where an application stands (tracker.py); NULL unless applied
    ("jobs", "stage_at", "TEXT"),
    ("answer_memory", "kind", "TEXT NOT NULL DEFAULT 'text'"),
    ("answer_memory", "options", "TEXT"),
    ("answer_memory", "confirmed", "INTEGER NOT NULL DEFAULT 1"),
    ("answer_memory", "overrides", "INTEGER NOT NULL DEFAULT 0"),
    ("answer_memory", "company", "TEXT NOT NULL DEFAULT ''"),
    ("answer_memory", "job_id", "TEXT NOT NULL DEFAULT ''"),
)
APP_JSON = ("questions", "answers", "report")

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
REFILTER_PENDING = "refilter_pending"   # meta key: set when the profile changes what the rules check


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
        have: dict[str, set[str]] = {}
        for table, column, definition in ADDED_COLUMNS:
            if table not in have:
                have[table] = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in have[table]:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        # Jobs marked applied before the tracker existed start at the Applied stage, on the day you marked them.
        with self.conn:
            rows = self.conn.execute("SELECT id, COALESCE(status_at, first_seen) FROM jobs"
                                     " WHERE status = 'applied' AND stage IS NULL").fetchall()
            for job_id, at in rows:
                self.conn.execute("UPDATE jobs SET stage = 'applied', stage_at = ? WHERE id = ?", (at, job_id))
                self.conn.execute("INSERT INTO track_events (job_id, kind, stage, at) VALUES (?, 'stage', 'applied', ?)",
                                  (job_id, at))

    def close(self) -> None:
        self.conn.close()

    def q(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

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

    def active_boards(self, since: str) -> set[str]:
        """Boards that produced a job the rules kept, first seen since `since`."""
        return {r[0] for r in self.conn.execute(
            "SELECT DISTINCT board FROM jobs WHERE rule_status IN ('pass', 'unclear') AND first_seen >= ?", (since,))}

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

    def get_profile(self) -> dict | None:
        row = self.conn.execute("SELECT data FROM profile WHERE id = 1").fetchone()
        return json.loads(row[0]) if row else None

    def save_profile(self, data: dict, now: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO profile (id, data, updated_at) VALUES (1, ?, ?)",
                              (json.dumps(data, ensure_ascii=False), now))

    def get_application(self, job_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()
        if not row:
            return None
        app = dict(row)
        for key in APP_JSON:
            app[key] = json.loads(app[key]) if app[key] else None
        return app

    def save_application(self, app: dict) -> None:
        """Insert or replace a whole application row."""
        row = {k: (json.dumps(v, ensure_ascii=False) if k in APP_JSON and v is not None else v) for k, v in app.items()}
        cols = ", ".join(row)
        with self.conn:
            self.conn.execute(f"INSERT OR REPLACE INTO applications ({cols}) VALUES ({', '.join('?' * len(row))})",
                              tuple(row.values()))

    def update_application(self, job_id: str, now: str, **fields) -> None:
        fields["updated_at"] = now
        sets = ", ".join(f"{k} = ?" for k in fields)
        values = [json.dumps(v, ensure_ascii=False) if k in APP_JSON and v is not None else v for k, v in fields.items()]
        with self.conn:
            self.conn.execute(f"UPDATE applications SET {sets} WHERE job_id = ?", (*values, job_id))

    def submitted_since(self, since: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM applications WHERE submitted_at >= ?", (since,)).fetchone()[0]

    def submitted_applications(self, limit: int = 20) -> list[dict]:
        """Applications sent through the agent, newest first, with their job's title and company."""
        rows = self.q("SELECT a.job_id, a.report, a.submitted_at, j.title, j.company FROM applications a"
                      " LEFT JOIN jobs j ON j.id = a.job_id WHERE a.submitted_at IS NOT NULL"
                      " ORDER BY a.submitted_at DESC LIMIT ?", (limit,))
        return [{**dict(r), "report": json.loads(r["report"]) if r["report"] else {}} for r in rows]

    # Saved answers (apply/memory.py).

    @staticmethod
    def _memory_row(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["value"] = json.loads(d["value"])
        d["options"] = json.loads(d["options"]) if d["options"] else []
        return d

    def memory_all(self) -> list[dict]:
        return [self._memory_row(r) for r in self.q("SELECT * FROM answer_memory ORDER BY updated_at DESC")]

    def memory_get(self, key: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM answer_memory WHERE key = ?", (key,)).fetchone()
        return self._memory_row(row) if row else None

    def memory_save(self, key: str, label: str, kind: str, options: list[str], value, confirmed: int, overrides: bool,
                    company: str, job_id: str, now: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO answer_memory (key, label, value, uses, updated_at, kind, options, confirmed, overrides,"
                " company, job_id) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET label = excluded.label, value = excluded.value, uses = uses + 1,"
                " updated_at = excluded.updated_at, kind = excluded.kind, options = excluded.options,"
                " confirmed = excluded.confirmed, overrides = excluded.overrides, company = excluded.company,"
                " job_id = excluded.job_id",
                (key, label, json.dumps(value, ensure_ascii=False), now, kind, json.dumps(options, ensure_ascii=False),
                 confirmed, int(overrides), company, job_id))

    def memory_edit(self, key: str, value, confirmed: int, now: str) -> bool:
        with self.conn:
            cur = self.conn.execute("UPDATE answer_memory SET value = ?, confirmed = ?, updated_at = ? WHERE key = ?",
                                    (json.dumps(value, ensure_ascii=False), confirmed, now, key))
        return cur.rowcount > 0

    def memory_delete(self, key: str) -> bool:
        with self.conn:
            return self.conn.execute("DELETE FROM answer_memory WHERE key = ?", (key,)).rowcount > 0

    def set_status(self, job_id: str, status: str, now: str) -> bool:
        """Applied starts a job on the tracker; moving it back off Applied (a mis-click) clears its history."""
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        with self.conn:
            row = self.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                return False
            self.conn.execute("UPDATE jobs SET status = ?, status_at = ? WHERE id = ?", (status, now, job_id))
            if status == "applied" and row[0] != "applied":
                self.conn.execute("UPDATE jobs SET stage = 'applied', stage_at = ? WHERE id = ?", (now, job_id))
                self.conn.execute("INSERT INTO track_events (job_id, kind, stage, at) VALUES (?, 'stage', 'applied', ?)",
                                  (job_id, now))
            elif status != "applied" and row[0] == "applied":
                self.conn.execute("UPDATE jobs SET stage = NULL, stage_at = NULL WHERE id = ?", (job_id,))
                self.conn.execute("DELETE FROM track_events WHERE job_id = ?", (job_id,))
        return True

    # The application tracker (tracker.py).

    def set_stage(self, job_id: str, stage: str, now: str, note: str = "") -> bool:
        with self.conn:
            cur = self.conn.execute("UPDATE jobs SET stage = ?, stage_at = ? WHERE id = ? AND status = 'applied'",
                                    (stage, now, job_id))
            if cur.rowcount:
                self.conn.execute("INSERT INTO track_events (job_id, kind, stage, note, at) VALUES (?, 'stage', ?, ?, ?)",
                                  (job_id, stage, note, now))
        return cur.rowcount == 1

    def add_note(self, job_id: str, note: str, now: str) -> bool:
        with self.conn:
            if not self.conn.execute("SELECT 1 FROM jobs WHERE id = ? AND status = 'applied'", (job_id,)).fetchone():
                return False
            self.conn.execute("INSERT INTO track_events (job_id, kind, note, at) VALUES (?, 'note', ?, ?)",
                              (job_id, note, now))
        return True

    def track_events(self, job_id: str | None = None) -> list[dict]:
        """Events oldest first: one job's, or every tracked job's."""
        where, params = ("WHERE job_id = ?", (job_id,)) if job_id else ("", ())
        return [dict(r) for r in self.q(f"SELECT * FROM track_events {where} ORDER BY at, id", params)]

    def tracked(self) -> list[dict]:
        """Every job you applied to, with when it last moved and whether the agent sent it."""
        return [dict(r) for r in self.q(
            "SELECT j.id, j.source, j.board, j.title, j.company, j.location, j.url, j.closed, j.status_at, j.stage,"
            " j.stage_at, j.ai_score, j.rule_score, a.submitted_at AS agent_at,"
            " (SELECT MAX(at) FROM track_events t WHERE t.job_id = j.id) AS active_at"
            " FROM jobs j LEFT JOIN applications a ON a.job_id = j.id AND a.submitted_at IS NOT NULL"
            " WHERE j.status = 'applied' ORDER BY j.stage_at DESC")]
