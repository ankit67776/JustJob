"""Dashboard sign-in over real HTTP, the run lock, and which boards the hourly refresh fetches."""
import http.client
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from justjob import pipeline, web
from justjob.db import DB
from justjob.lock import run_lock
from justjob.models import BoardResult, Job

from .helpers import make_cfg


class SignInTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "jobs.db"
        db = DB(db_path)
        db.insert_jobs([Job("greenhouse", "1", "AI Engineer", "Acme", "https://x/1", "Remote", True,
                            "2+ years of Python", None, "", "greenhouse:acme")], "2026-01-01T00:00:00+00:00")
        pipeline.apply_rules(make_cfg(), db)
        db.close()
        saved = {k: getattr(web.Handler, k) for k in ("db_path", "cfg", "password", "key", "failures")}
        self.addCleanup(lambda: [setattr(web.Handler, k, v) for k, v in saved.items()])
        web.Handler.db_path, web.Handler.cfg, web.Handler.failures = str(db_path), make_cfg(), {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def use_password(self, pw):
        web.Handler.password = pw
        web.Handler.key = web.session_key(pw, b"s" * 32)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        hdrs = dict(headers or {})
        if body is not None:
            body = urllib.parse.urlencode(body)
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        out = (resp.status, dict(resp.getheaders()), resp.read().decode("utf-8"))
        conn.close()
        return out

    def test_no_password_is_open_locally_but_not_through_a_proxy(self):
        web.Handler.password = ""
        self.assertEqual(self.request("GET", "/")[0], 200)
        self.assertEqual(self.request("GET", "/", headers={"X-Forwarded-For": "203.0.113.9"})[0], 403)

    def test_sign_in_flow(self):
        self.use_password("correct horse")
        status, headers, _ = self.request("GET", "/?role=ai")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/login?next=%2F%3Frole%3Dai")
        self.assertEqual(self.request("GET", "/description?id=greenhouse:1")[0], 401)
        self.assertEqual(self.request("POST", "/status", {"id": "greenhouse:1", "status": "hidden"})[0], 401)

        status, _, page = self.request("POST", "/login", {"password": "wrong", "next": "/"})
        self.assertEqual(status, 401)
        self.assertIn("Wrong password. Try again.", page)

        status, headers, _ = self.request("POST", "/login", {"password": "correct horse", "next": "/?role=ai"},
                                          headers={"X-Forwarded-Proto": "https"})
        self.assertEqual((status, headers["Location"]), (303, "/?role=ai"))
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        session = {"Cookie": cookie.split(";")[0]}
        status, _, page = self.request("GET", "/", headers=session)
        self.assertEqual(status, 200)
        self.assertIn('href="/logout"', page)
        self.assertEqual(self.request("POST", "/status", {"id": "greenhouse:1", "status": "hidden"}, session)[0], 200)

        self.use_password("a new password")   # changing it signs everyone out
        self.assertEqual(self.request("GET", "/", headers=session)[0], 303)

    def test_wrong_passwords_are_throttled(self):
        self.use_password("pw")
        web.Handler.failures = {"127.0.0.1": [9e18] * web.MAX_FAILURES}   # as if just failed 5 times
        status, _, page = self.request("POST", "/login", {"password": "pw"})
        self.assertEqual(status, 429)
        self.assertIn("Too many wrong passwords", page)

    def test_sessions_and_redirects(self):
        key = web.session_key("pw", b"k" * 32)
        good = web.make_session(key, now=1000)
        self.assertTrue(web.valid_session(key, good, now=2000))
        self.assertFalse(web.valid_session(key, good, now=1000 + web.SESSION_DAYS * 86400 + 1))   # expired
        self.assertFalse(web.valid_session(key, good[:-1] + ("0" if good[-1] != "0" else "1"), now=2000))
        self.assertFalse(web.valid_session(web.session_key("other", b"k" * 32), good, now=2000))
        self.assertFalse(web.valid_session(key, "garbage", now=2000))
        for url, safe in [("/?q=x", "/?q=x"), ("//evil.com", "/"), ("https://evil.com", "/"), ("/\\evil.com", "/")]:
            self.assertEqual(web.safe_next(url), safe, url)


class LockTests(unittest.TestCase):
    def test_second_run_waits_or_skips(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "run.lock"
            with run_lock(path) as first:
                self.assertTrue(first)
                with run_lock(path, wait=0.3) as second:
                    self.assertFalse(second)
            with run_lock(path) as again:   # released when the first run ended
                self.assertTrue(again)


class HotRefreshTests(unittest.TestCase):
    def test_hot_tasks(self):
        cfg = make_cfg()
        cfg["sources"] = {"greenhouse": {"enabled": True, "boards": {"picked": "Picked"}}}
        db = DB(":memory:")
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=200)).isoformat(timespec="seconds")
        db.insert_jobs([Job("greenhouse", "1", "AI Engineer", "Hot", "https://x/1", "Remote", True,
                            "2+ years of Python", None, "", "greenhouse:hot")], now.isoformat(timespec="seconds"))
        db.insert_jobs([Job("greenhouse", "2", "AI Engineer", "Stale", "https://x/2", "Remote", True,
                            "2+ years of Python", None, "", "greenhouse:stale")], old)
        db.insert_jobs([Job("greenhouse", "3", "Accountant", "Quiet", "https://x/3", "Remote", True,
                            "", None, "", "greenhouse:quiet")], now.isoformat(timespec="seconds"))
        db.insert_jobs([Job("workable", "4", "AI Engineer", "Budget", "https://x/4", "Remote", True,
                            "2+ years of Python", None, "", "workable:budget")], now.isoformat(timespec="seconds"))
        pipeline.apply_rules(cfg, db)
        labels = ["greenhouse:hot", "greenhouse:stale", "greenhouse:quiet", "greenhouse:picked", "greenhouse:other",
                  "workable:budget", "remotive", "hackernews", "jsearch:AI engineer", "adzuna:ai engineer"]
        tasks = [(label, lambda ctx, label=label: BoardResult(label, [], True)) for label in labels]
        chosen = [label for label, _ in pipeline.hot_tasks(cfg, db, tasks)]
        db.close()
        self.assertEqual(chosen, ["greenhouse:hot", "greenhouse:picked", "remotive", "hackernews"])

    def test_hot_run_skips_digest(self):
        db = DB(":memory:")
        with mock.patch.object(pipeline, "build_tasks", return_value=[]), \
                mock.patch.object(pipeline.digest, "write") as write:
            stats = pipeline.run(make_cfg(), db, use_ai=False, send=False, hot=True)
            self.assertTrue(stats["hot"])
            self.assertNotIn("digest", stats)
            write.assert_not_called()
        db.close()


if __name__ == "__main__":
    unittest.main()
