"""AI step end to end against a fake OpenAI-compatible server."""
import json
import os
import re
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from justjob import ai, pipeline
from justjob.db import DB
from justjob.models import Job

from .helpers import make_cfg


class FakeAPI(BaseHTTPRequestHandler):
    calls: list = []
    garbage_first = True

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAPI.calls.append(body)
        blocks = re.findall(r"### job_id: (\d+)\nTitle: ([^\n]*)", body["messages"][1]["content"])
        if FakeAPI.garbage_first:
            FakeAPI.garbage_first = False
            content = "I think these jobs are great!"
        else:
            content = json.dumps({"results": [
                {"job_id": i, "min_years_required": 5 if "Billing" in title else 1,
                 "is_within_limit": "Billing" not in title, "match_score": 70, "reason": "ok", "flags": []}
                for i, title in blocks]})
        out = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class AIRunTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.cfg = make_cfg()
        self.cfg["ai"].update(provider="fake", fallback=[], batch_size=2, min_interval_seconds=0)
        self.cfg["ai"]["providers"]["fake"] = {
            "base_url": f"http://127.0.0.1:{self.server.server_port}/v1", "model": "m", "api_key_env": "JF_FAKE_KEY"}
        os.environ["JF_FAKE_KEY"] = "x"
        self.db = DB(":memory:")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.db.close()

    def test_run(self):
        jobs = [Job("greenhouse", str(i), f"Software Engineer {name}", "Acme", f"https://x/{i}",
                    "Remote", True, "We build things.", None, "", "greenhouse:acme")
                for i, name in enumerate(["Search", "Billing", "Infra"])]
        self.db.insert_jobs(jobs, "2026-01-01T00:00:00+00:00")
        pipeline.apply_rules(self.cfg, self.db)
        pipeline.dedup(self.db)

        stats = ai.run(self.cfg, self.db)
        self.assertEqual(stats["analyzed"], 3)
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(len(FakeAPI.calls), 3)   # 2 batches + 1 retry after the garbage reply
        self.assertEqual(FakeAPI.calls[0]["response_format"], {"type": "json_object"})
        within = dict(self.db.q("SELECT title, ai_within FROM jobs"))
        self.assertEqual(sum(within.values()), 2)   # the "5 years" job is marked out of range


if __name__ == "__main__":
    unittest.main()
