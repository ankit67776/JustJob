"""End-to-end pipeline over a fake source and an in-memory database."""
import unittest
from unittest import mock

from justjob import pipeline
from justjob.db import DB, SHOWABLE
from justjob.models import BoardResult, Job

from .helpers import make_cfg


def job(i, title, company="Acme", desc="2+ years of Python experience", source="greenhouse", board="greenhouse:acme"):
    return Job(source=source, source_id=str(i), title=title, company=company, url=f"https://x/{i}",
               location="Remote", remote=True, description=desc, board=board)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.db = DB(":memory:")

    def tearDown(self):
        self.db.close()

    def fetch_with(self, results):
        tasks = [(r.board, (lambda r=r: (lambda ctx: r))()) for r in results]
        with mock.patch.object(pipeline, "build_tasks", return_value=tasks):
            return pipeline.fetch(self.cfg, self.db)

    def test_flow(self):
        board = [
            job(1, "Software Engineer"),
            job(2, "Staff Software Engineer"),                                    # title-rejected, not stored
            job(3, "ML Engineer", desc="6+ years of experience required"),         # stored, rule-rejected
        ]
        other = [job(9, "Software Engineer (Remote)", source="remotive", board="remotive")]   # duplicate of 1
        stats = self.fetch_with([BoardResult("greenhouse:acme", board, True),
                                 BoardResult("remotive", other, False)])
        self.assertEqual(stats["new"], 3)
        pipeline.apply_rules(self.cfg, self.db)
        pipeline.dedup(self.db)
        shown = [r["id"] for r in self.db.q(f"SELECT id FROM jobs WHERE {SHOWABLE}")]
        self.assertEqual(shown, ["greenhouse:1"])   # direct board copy wins over the aggregator

        # Next day job 1 is gone from the board -> closed, so the Remotive copy takes over.
        self.fetch_with([BoardResult("greenhouse:acme", [job(3, "ML Engineer")], True)])
        pipeline.dedup(self.db)
        self.assertEqual(self.db.q("SELECT closed FROM jobs WHERE id = 'greenhouse:1'")[0][0], 1)
        shown = [r["id"] for r in self.db.q(f"SELECT id FROM jobs WHERE {SHOWABLE}")]
        self.assertEqual(shown, ["remotive:9"])

    def test_failing_board_rests(self):
        def broken(ctx):
            raise ConnectionError("HTTP 404")
        ok = BoardResult("greenhouse:acme", [job(1, "Software Engineer")], True)
        tasks = [("greenhouse:gone", broken), ("greenhouse:acme", lambda ctx: ok)]
        with mock.patch.object(pipeline, "build_tasks", return_value=tasks):
            for _ in range(3):
                stats = pipeline.fetch(self.cfg, self.db)
                self.assertEqual((stats["errors"], stats["skipped"]), (1, 0))
            stats = pipeline.fetch(self.cfg, self.db)   # 3 failures in a row: skipped for a day
        self.assertEqual((stats["errors"], stats["skipped"], stats["tasks"]), (0, 1, 1))
        row = self.db.q("SELECT fails, error FROM boards WHERE board = 'greenhouse:gone'")[0]
        self.assertEqual((row["fails"], row["error"]), (3, "HTTP 404"))
        self.assertEqual(self.db.q("SELECT listed FROM boards WHERE board = 'greenhouse:acme'")[0][0], 1)

    def test_rate_limited_source_stops_without_marking_boards(self):
        from justjob.net import HttpError

        def limited(ctx):
            raise HttpError(429, "https://apply.workable.com/x", "", retry_after=83000)

        def gone(ctx):
            raise HttpError(404, "https://boards-api.greenhouse.io/x", "")
        tasks = [(f"workable:co{i}", limited) for i in range(10)] + [("greenhouse:gone", gone)]
        with mock.patch.object(pipeline, "build_tasks", return_value=tasks):
            stats = pipeline.fetch(self.cfg, self.db)
        self.assertEqual((stats["rate_limited"], stats["errors"]), (10, 1))
        boards = dict(self.db.q("SELECT board, fails FROM boards"))
        self.assertEqual(boards, {"greenhouse:gone": 1})   # 429s aren't held against the boards

    def test_one_short_rate_limit_does_not_stop_source(self):
        from justjob.net import HttpError

        def busy(ctx):
            raise HttpError(429, "https://acme.wd1.myworkdayjobs.com/x", "")
        ok = [(f"workday:co{i}/wd1/ext", (lambda i: lambda ctx: BoardResult(f"workday:co{i}/wd1/ext", [], True))(i))
              for i in range(6)]
        with mock.patch.object(pipeline, "build_tasks", return_value=[("workday:busy/wd1/ext", busy)] + ok):
            stats = pipeline.fetch(self.cfg, self.db)
        self.assertEqual(stats["rate_limited"], 1)
        self.assertEqual(self.db.q("SELECT COUNT(*) FROM boards WHERE fails = 0")[0][0], 6)

    def test_rotation_caps_boards_per_run(self):
        self.cfg["sources"] = {"workable": {"max_boards": 2}}
        seen = []
        tasks = [(f"workable:co{i}", (lambda i: lambda ctx: seen.append(i) or BoardResult(f"workable:co{i}", [], True))(i))
                 for i in range(5)]
        with mock.patch.object(pipeline, "build_tasks", return_value=tasks):
            for _ in range(3):
                pipeline.fetch(self.cfg, self.db)
        self.assertEqual(sorted(seen[:2]), [0, 1])
        self.assertEqual(sorted(seen[2:4]), [2, 3])            # least recently checked go next
        self.assertEqual(sorted(seen), [0, 0, 1, 2, 3, 4])

    def test_partial_listing_closes_only_long_unseen_jobs(self):
        board = "workday:acme/wd1/ext"
        jobs = [job(i, "Software Engineer", source="workday", board=board) for i in (1, 2)]
        self.fetch_with([BoardResult(board, jobs, True)])
        with self.db.conn:
            self.db.conn.execute("UPDATE jobs SET last_seen = '2000-01-01' WHERE id = 'workday:2'")
        # Partial listing without job 2 or job 1: only job 2 (unseen for ages) closes.
        self.fetch_with([BoardResult(board, [], False, listed_ids=[])])
        closed = dict(self.db.q("SELECT id, closed FROM jobs"))
        self.assertEqual(closed, {"workday:1": 0, "workday:2": 1})

    def test_applied_job_keeps_duplicates_hidden(self):
        self.fetch_with([BoardResult("greenhouse:acme", [job(1, "Software Engineer")], True)])
        pipeline.apply_rules(self.cfg, self.db)
        pipeline.dedup(self.db)
        self.db.set_status("greenhouse:1", "applied", "2026-01-01")
        self.fetch_with([BoardResult("greenhouse:acme", [], True),
                         BoardResult("remotive", [job(9, "Software Engineer", source="remotive", board="remotive")], False)])
        pipeline.apply_rules(self.cfg, self.db)
        pipeline.dedup(self.db)
        new_shown = self.db.q(f"SELECT id FROM jobs WHERE {SHOWABLE} AND status = 'new'")
        self.assertEqual(new_shown, [])


if __name__ == "__main__":
    unittest.main()
