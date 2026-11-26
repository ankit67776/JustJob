"""The application tracker: stages, history, follow-up nudges, hand-added applications, insights and pages."""
import argparse
import http.client
import tempfile
import threading
import unittest
import urllib.parse
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

from justjob import cli, pipeline, tracker, web
from justjob.db import DB
from justjob.models import Job

from .helpers import make_cfg

NOW = datetime.now(UTC).replace(microsecond=0)   # the dashboard reads the real clock


def ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat(timespec="seconds")


def job(n: int, source="greenhouse", title="AI Engineer") -> Job:
    return Job(source, f"acme/{n}", title, f"Co{n}", f"https://x/{n}", "Remote", True, "Python", None, "", f"{source}:co{n}")


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.db.insert_jobs([job(i) for i in range(1, 7)], ago(60))

    def tearDown(self):
        self.db.close()

    def stage(self, job_id):
        return self.db.q("SELECT stage FROM jobs WHERE id = ?", (job_id,))[0][0]

    def test_applied_starts_the_timeline_and_moving_back_clears_it(self):
        self.db.set_status("greenhouse:acme/1", "applied", ago(10))
        self.assertEqual(self.stage("greenhouse:acme/1"), "applied")
        tracker.set_stage(self.db, "greenhouse:acme/1", "screening", ago(5), "Recruiter call")
        self.db.set_status("greenhouse:acme/1", "applied", ago(4))          # marking again adds nothing
        events = self.db.track_events("greenhouse:acme/1")
        self.assertEqual([(ev["stage"], ev["note"]) for ev in events], [("applied", ""), ("screening", "Recruiter call")])
        self.db.set_status("greenhouse:acme/1", "new", ago(1))              # a mis-click undone
        self.assertIsNone(self.stage("greenhouse:acme/1"))
        self.assertEqual(self.db.track_events("greenhouse:acme/1"), [])

    def test_only_applied_jobs_move_and_stages_are_checked(self):
        self.assertFalse(tracker.set_stage(self.db, "greenhouse:acme/2", "interview", ago(1)))
        with self.assertRaises(ValueError):
            tracker.set_stage(self.db, "greenhouse:acme/2", "hired", ago(1))
        self.assertFalse(tracker.add_note(self.db, "greenhouse:acme/2", "hello", ago(1)))
        self.db.set_status("greenhouse:acme/2", "applied", ago(1))
        self.assertFalse(tracker.add_note(self.db, "greenhouse:acme/2", "   ", ago(1)))
        self.assertTrue(tracker.add_note(self.db, "greenhouse:acme/2", "Sent a thank-you note", ago(1)))

    def test_jobs_applied_before_the_tracker_start_at_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.db"
            db = DB(path)
            db.insert_jobs([job(1)], ago(60))
            with db.conn:   # how a database from before the tracker looks
                db.conn.execute("UPDATE jobs SET status = 'applied', status_at = ? WHERE id = 'greenhouse:acme/1'",
                                (ago(9),))
            db.close()
            db = DB(path)
            row = db.tracked()[0]
            self.assertEqual((row["stage"], row["stage_at"]), ("applied", ago(9)))
            self.assertEqual(len(db.track_events("greenhouse:acme/1")), 1)
            db.close()
            db = DB(path)
            self.assertEqual(len(db.track_events("greenhouse:acme/1")), 1)   # once only
            db.close()

    def test_nudges(self):
        row = {"stage": "applied", "stage_at": ago(3), "active_at": ago(3)}
        self.assertIsNone(tracker.nudge(row, NOW.isoformat()))
        self.assertEqual(tracker.nudge({**row, "active_at": ago(8)}, NOW.isoformat()), "follow_up")
        self.assertEqual(tracker.nudge({**row, "active_at": ago(22)}, NOW.isoformat()), "no_reply")
        self.assertEqual(tracker.nudge({**row, "stage": "interview", "active_at": ago(22)}, NOW.isoformat()), "follow_up")
        for done in ("offer", "rejected", "no_response"):
            self.assertIsNone(tracker.nudge({**row, "stage": done, "active_at": ago(40)}, NOW.isoformat()))

    def test_applications_made_elsewhere(self):
        job_id = tracker.add_manual(self.db, "Razorpay", "AI Engineer", "razorpay.com/jobs/1", "Bengaluru", "Referral",
                                    ago(3), ago(3))
        row = next(r for r in self.db.tracked() if r["id"] == job_id)
        self.assertEqual((row["source"], row["url"], tracker.where_from(row)),
                         ("manual", "https://razorpay.com/jobs/1", "Referral"))
        self.assertEqual(row["status_at"], ago(3))
        pipeline.apply_rules(make_cfg(), self.db, everything=True)          # never filtered or scored
        self.assertIsNone(self.db.q("SELECT rule_status FROM jobs WHERE id = ?", (job_id,))[0][0])
        with self.assertRaises(ValueError):
            tracker.add_manual(self.db, " ", "AI Engineer", "", "", "", ago(1), ago(1))

    def test_insights(self):
        self.db.insert_jobs([job(7, title="Frontend Developer")], ago(60))
        for i, applied in ((1, 40), (2, 20), (3, 12), (4, 10), (5, 3), (7, 2)):
            self.db.set_status(f"greenhouse:acme/{i}", "applied", ago(applied))
        tracker.set_stage(self.db, "greenhouse:acme/1", "screening", ago(36))
        tracker.set_stage(self.db, "greenhouse:acme/1", "interview", ago(30))
        tracker.set_stage(self.db, "greenhouse:acme/1", "offer", ago(25))
        tracker.set_stage(self.db, "greenhouse:acme/2", "interview", ago(14))   # straight to interview
        tracker.set_stage(self.db, "greenhouse:acme/2", "rejected", ago(10))
        tracker.set_stage(self.db, "greenhouse:acme/3", "rejected", ago(10))    # a rejection is a reply
        tracker.set_stage(self.db, "greenhouse:acme/4", "withdrawn", ago(9))    # withdrawing isn't
        tracker.add_note(self.db, "greenhouse:acme/5", "Pinged the recruiter", ago(1))
        ins = tracker.insights(self.db, NOW.isoformat())
        self.assertEqual((ins["total"], ins["replied"], ins["interviews"], ins["offers"], ins["open"]), (6, 3, 2, 1, 3))
        self.assertEqual([f["count"] for f in ins["funnel"]], [6, 2, 2, 1])   # reaching interview passed screening
        self.assertEqual(ins["funnel"][2]["of_previous"], 1.0)
        self.assertEqual(ins["median_days"], 4)                                 # replies after 4, 6 and 2 days
        self.assertEqual(ins["outcomes"], {"accepted": 0, "rejected": 2, "withdrawn": 1, "no_response": 0})
        roles = {g["name"]: (g["applied"], g["replied"]) for g in ins["by_role"]}
        self.assertEqual(roles, {"AI / ML": (5, 3), "Frontend": (1, 0)})
        self.assertEqual(sum(w["count"] for w in ins["weeks"]), 6)
        recent = tracker.insights(self.db, NOW.isoformat(), days=15)
        self.assertEqual((recent["total"], recent["replied"]), (4, 1))


class TrackerPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "jobs.db"
        db = DB(self.db_path)
        db.insert_jobs([job(1), job(2)], ago(30))
        db.set_status("greenhouse:acme/1", "applied", ago(45))
        db.close()
        saved = {k: getattr(web.Handler, k) for k in ("db_path", "cfg", "password")}
        self.addCleanup(lambda: [setattr(web.Handler, k, v) for k, v in saved.items()])
        web.Handler.db_path, web.Handler.cfg, web.Handler.password = str(self.db_path), make_cfg(), ""
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, fields=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        body = urllib.parse.urlencode(fields) if fields is not None else None
        conn.request(method, path, body=body, headers={"Content-Type": "application/x-www-form-urlencoded"} if body else {})
        resp = conn.getresponse()
        out = (resp.status, resp.getheader("Location"), resp.read().decode())
        conn.close()
        return out

    def test_board_shows_cards_by_stage_with_nudges(self):
        status, _, page = self.request("GET", "/tracker")
        self.assertEqual(status, 200)
        self.assertIn('aria-current="page">Applications', page)
        self.assertIn('data-id="greenhouse:acme/1" data-stage="applied"', page)
        self.assertIn("No reply in 3 weeks", page)
        self.assertNotIn("greenhouse:acme/2", page)                          # not applied

    def test_moving_noting_and_the_job_page(self):
        link = "/tracker/job?" + urllib.parse.urlencode({"id": "greenhouse:acme/1"})
        self.assertEqual(self.request("POST", "/tracker/stage", {"id": "greenhouse:acme/1", "stage": "interview"})[1],
                         "/tracker?done=moved")
        self.assertEqual(self.request("POST", "/tracker/stage", {"id": "greenhouse:acme/1", "stage": "offer", "next": "job",
                                                                 "note": "Verbal offer"})[1], link)
        self.assertEqual(self.request("POST", "/tracker/note", {"id": "greenhouse:acme/1", "note": "Negotiate notice"})[1],
                         link)
        status, _, page = self.request("GET", link)
        self.assertEqual(status, 200)
        for text in ("Moved to Interview", "Moved to Offer", "Verbal offer", "Negotiate notice"):
            self.assertIn(text, page)
        self.assertEqual(self.request("POST", "/tracker/stage", {"id": "greenhouse:acme/1", "stage": "hired"})[0], 400)
        self.assertEqual(self.request("POST", "/tracker/stage", {"id": "greenhouse:acme/2", "stage": "offer"})[0], 404)
        self.assertEqual(self.request("GET", "/tracker/job?id=nope")[1], "/tracker")

    def test_adding_an_application_made_elsewhere(self):
        status, where, _ = self.request("POST", "/tracker/add", {"company": "Razorpay", "title": "AI Engineer",
                                                                 "via": "Referral", "applied": "2026-11-20"})
        self.assertEqual(status, 303)
        self.assertTrue(where.startswith("/tracker/job?id=manual%3A"))
        page = self.request("GET", where)[2]
        self.assertIn("Razorpay", page)
        self.assertIn("Applied 20 Nov 2026", page)
        self.assertEqual(self.request("POST", "/tracker/add", {"company": "", "title": "x"})[1], "/tracker?done=missing&add=1")

    def test_insights_page(self):
        status, _, page = self.request("GET", "/insights?range=all")
        self.assertEqual(status, 200)
        self.assertIn("How far applications got", page)
        self.assertIn("Show as a table", page)
        self.assertIn("No applications in this range", self.request("GET", "/insights?range=30")[2])


class StageCommandTests(unittest.TestCase):
    def test_stage_command(self):
        db = DB(":memory:")
        db.insert_jobs([job(1)], ago(10))
        db.set_status("greenhouse:acme/1", "applied", ago(5))
        cli.cmd_stage(make_cfg(), db, argparse.Namespace(id="acme/1", stage="screening", note="Phone screen"))
        self.assertEqual(db.track_events("greenhouse:acme/1")[-1]["note"], "Phone screen")
        db.set_status("greenhouse:acme/1", "new", ago(1))
        with self.assertRaises(SystemExit):
            cli.cmd_stage(make_cfg(), db, argparse.Namespace(id="acme/1", stage="offer", note=""))
        db.close()


if __name__ == "__main__":
    unittest.main()
