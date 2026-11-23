"""Profile storage, the Profile form, resume reading and the profile routes."""
import http.client
import io
import json
import tempfile
import threading
import unittest
import urllib.parse
import zipfile
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from justjob import ai, pipeline, web
from justjob import profile as P
from justjob.config import resume_text
from justjob.db import DB, REFILTER_PENDING

from .helpers import make_cfg

RESUME = ("Asha Rao\nasha@example.com | +91 90000 00000 | Bengaluru, India\n"
          "AI Engineer at Acme since July 2023. Built RAG search with Python, FastAPI and PyTorch. " * 3)


def docx(text: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in text.split("\n"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="x"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


LLM_FACTS = {
    "first_name": "Asha", "last_name": "Rao", "email": "asha@example.com", "phone": "+91 90000 00000",
    "location": "Bengaluru, India", "linkedin": "linkedin.com/in/asha", "github": None, "portfolio": "not a url",
    "summary": "I build LLM apps.", "skills": ["Python", "FastAPI", "python", "", 42],
    "experience": [
        {"title": "AI Engineer", "company": "Acme", "start": "2023-07", "end": "Present", "internship": False,
         "highlights": ["Cut latency 40%", None]},
        {"title": "Backend Developer", "company": "Beta", "start": "2021-01", "end": "2023-06", "internship": False},
        {"title": "Intern", "company": "Gamma", "start": "2020-05", "end": "2020-08", "internship": True},
        {"title": "", "company": "Nameless"},
        "not a job",
    ],
    "education": [{"school": "IIT", "degree": "B.Tech", "field": "CS", "start": "2016", "end": "Expected 2020"}],
    "projects": [{"name": "RAG bot", "tech": ["LangChain"], "url": "github.com/asha/rag"}],
    "certifications": "AWS",
}


class ProfileModelTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.db = DB(":memory:")

    def tearDown(self):
        self.db.close()

    def test_defaults_come_from_config_and_decline_voluntary_questions(self):
        prof = P.load(self.db, self.cfg)
        self.assertEqual(prof["skills"], self.cfg["profile"]["skills"])
        self.assertEqual(prof["years_experience"], 3)
        self.assertEqual(prof["gender"], P.DECLINE)
        self.assertEqual(prof["first_name"], "")
        self.assertIsNone(prof["notice_period_days"])

    def test_saved_values_win_and_reach_the_config(self):
        prof = P.load(self.db, self.cfg)
        prof.update(skills=["go"], max_years_required=2, resume_text="My resume")
        P.save(self.db, prof)
        cfg = make_cfg()
        P.apply_to_cfg(cfg, P.load(self.db, cfg))
        self.assertEqual(cfg["profile"]["skills"], ["go"])
        self.assertEqual(cfg["profile"]["max_years_required"], 2)
        self.assertEqual(resume_text(cfg), "My resume")

    def test_form_parsing(self):
        current = P.load(self.db, self.cfg)
        form = {"first_name": ["  Asha "], "skills": ["python, React;\nSQL, python"], "years_experience": ["3.5"],
                "notice_period_days": ["abc"], "sponsorship_needed": ["Maybe"], "linkedin": ["linkedin.com/in/a"],
                "email": ["not-an-email"], "max_years_required": [""]}
        new, bad = P.from_form(form, current)
        self.assertEqual(new["first_name"], "Asha")
        self.assertEqual(new["skills"], ["python", "React", "SQL"])
        self.assertEqual(new["years_experience"], 3.5)
        self.assertEqual(new["sponsorship_needed"], "")
        self.assertEqual(new["linkedin"], "https://linkedin.com/in/a")
        self.assertIsNone(new["max_years_required"])              # cleared
        self.assertEqual(new["avoid"], current["avoid"])          # not on the form: kept
        self.assertEqual(sorted(bad), ["email", "notice_period_days"])
        self.assertTrue(P.rules_changed(current, new))

    def test_missing_lists_what_the_agent_needs(self):
        prof = P.load(self.db, self.cfg)
        labels = [label for _, label in P.missing(prof)]
        self.assertIn("Resume", labels)
        self.assertIn("Expected CTC", labels)
        self.assertNotIn("Years of experience", labels)           # seeded from config
        self.assertEqual(P.required_count(), len(labels) + 1)


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.db = DB(":memory:")
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_extract_text_from_docx_and_txt(self):
        self.assertEqual(P.extract_text("cv.docx", docx("Asha Rao\nAI   Engineer")), "Asha Rao\nAI Engineer")
        self.assertEqual(P.extract_text("cv.TXT", b"Hello\n\n\n\nthere"), "Hello\n\nthere")
        with self.assertRaises(P.ResumeError):
            P.extract_text("cv.exe", b"MZ")
        with self.assertRaises(P.ResumeError):
            P.extract_text("cv.docx", b"not a zip")

    def test_store_resume_replaces_the_old_one_and_drops_old_facts(self):
        P.store_resume(self.db, self.cfg, self.dir, "first.txt", RESUME.encode())
        prof = P.load(self.db, self.cfg)
        prof["facts"] = {"skills": []}
        P.save(self.db, prof)
        prof = P.store_resume(self.db, self.cfg, self.dir, "Asha CV.docx", docx(RESUME))
        self.assertEqual([f.name for f in (self.dir / "resume").iterdir()], ["resume.docx"])
        self.assertEqual(prof["resume"]["name"], "Asha CV.docx")
        self.assertIsNone(P.load(self.db, self.cfg)["facts"])
        self.assertIn("RAG search", P.load(self.db, self.cfg)["resume_text"])
        with self.assertRaises(P.ResumeError):
            P.store_resume(self.db, self.cfg, self.dir, "short.txt", b"too short")

    def test_clean_facts_drops_malformed_values(self):
        facts = P.clean_facts(LLM_FACTS)
        self.assertEqual(facts["skills"], ["Python", "FastAPI", "42"])
        self.assertEqual([j["company"] for j in facts["experience"]], ["Acme", "Beta", "Gamma"])
        self.assertEqual(facts["experience"][0]["highlights"], ["Cut latency 40%"])
        self.assertEqual(facts["linkedin"], "https://linkedin.com/in/asha")
        self.assertIsNone(facts["portfolio"])
        self.assertEqual(facts["education"][0]["end"], "2020")
        self.assertEqual(facts["certifications"], [])            # a string, not a list
        with self.assertRaises(ValueError):
            P.clean_facts(["not", "an", "object"])

    def test_years_from_dates_skips_internships_and_counts_overlaps_once(self):
        jobs = [{"start": "2021-01", "end": "2022-12"}, {"start": "2022-07", "end": "2023-06"},
                {"start": "2019-01", "end": "2020-12", "internship": True},
                {"start": "2024-01", "end": "Present"}, {"start": None, "end": "2020-01"}]
        self.assertEqual(P.years_from(jobs, date(2024, 12, 15)), 3.5)   # 30 months + 12 months
        self.assertIsNone(P.years_from([], date(2024, 1, 1)))

    def test_fill_only_touches_empty_fields(self):
        prof = P.load(self.db, self.cfg)
        prof["email"] = "mine@example.com"
        filled = P.fill_from_facts(prof, P.clean_facts(LLM_FACTS), "fake/m", date(2026, 7, 1))
        self.assertEqual(prof["email"], "mine@example.com")
        self.assertEqual(prof["first_name"], "Asha")
        self.assertEqual((prof["current_title"], prof["current_company"]), ("AI Engineer", "Acme"))
        self.assertEqual(prof["years_experience"], 3)            # seeded from config, so kept
        self.assertEqual(prof["skills"], self.cfg["profile"]["skills"])
        self.assertIn("First name", filled)
        self.assertNotIn("Email", filled)
        self.assertEqual(P.new_skills(prof), ["42"])   # Python and FastAPI are already there

    def test_parse_resume_retries_bad_json_then_validates(self):
        fake = ai.Provider("fake", "http://x", "m", "k")
        replies = iter(["sorry, no json", json.dumps(LLM_FACTS)])
        with mock.patch.object(ai, "providers", return_value=[fake]), \
                mock.patch.object(ai, "chat", side_effect=lambda p, m: next(replies)):
            facts, model = P.parse_resume(self.cfg, RESUME)
        self.assertEqual(model, "fake/m")
        self.assertEqual(facts["first_name"], "Asha")
        with mock.patch.object(ai, "providers", return_value=[]), self.assertRaises(P.ResumeError):
            P.parse_resume(self.cfg, RESUME)


class RefilterTests(unittest.TestCase):
    def test_refilter_clears_the_pending_flag(self):
        db = DB(":memory:")
        db.set_meta(REFILTER_PENDING, "1")
        pipeline.refilter(make_cfg(), db)
        self.assertFalse(db.get_meta(REFILTER_PENDING))
        db.close()


class ProfileRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "jobs.db"
        DB(self.db_path).close()
        saved = {k: getattr(web.Handler, k) for k in ("db_path", "cfg", "password", "on_rules_changed")}
        self.addCleanup(lambda: [setattr(web.Handler, k, v) for k, v in saved.items()])
        self.hook = mock.Mock()
        web.Handler.db_path, web.Handler.cfg, web.Handler.password = str(self.db_path), make_cfg(), ""
        web.Handler.on_rules_changed = staticmethod(self.hook)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        out = (resp.status, dict(resp.getheaders()), resp.read())
        conn.close()
        return out

    def post_form(self, fields, headers=None):
        return self.request("POST", "/profile", urllib.parse.urlencode(fields),
                            {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})

    def stored(self):
        db = DB(self.db_path)
        try:
            return P.load(db, make_cfg()), db.get_meta(REFILTER_PENDING)
        finally:
            db.close()

    def test_page_renders_with_nav_and_readiness(self):
        status, _, body = self.request("GET", "/profile")
        page = body.decode()
        self.assertEqual(status, 200)
        self.assertIn('aria-current="page">Profile', page)
        self.assertIn("Ready to apply", page)
        self.assertIn('id="f-expected_ctc"', page)

    def test_save_without_rule_changes(self):
        status, headers, _ = self.post_form({"first_name": "Asha", "skills": "python, pytorch, llm, fastapi, react"})
        self.assertEqual((status, headers["Location"]), (303, "/profile?done=saved"))
        prof, pending = self.stored()
        self.assertEqual(prof["first_name"], "Asha")
        self.assertFalse(pending)
        self.hook.assert_not_called()

    def test_changing_skills_queues_a_refilter_and_updates_the_running_config(self):
        status, headers, _ = self.post_form({"skills": "go, rust", "years_experience": "x"})
        self.assertEqual(headers["Location"], "/profile?done=saved-refilter&bad=years_experience")
        self.assertEqual(self.stored()[1], "1")
        self.hook.assert_called_once()
        self.assertEqual(web.Handler.cfg["profile"]["skills"], ["go", "rust"])
        page = self.request("GET", headers["Location"])[2].decode()
        self.assertIn("checked again", page)
        self.assertIn("Years of experience", page)

    def test_other_sites_cannot_post(self):
        status, _, _ = self.post_form({"first_name": "Evil"}, {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.stored()[0]["first_name"], "")

    def test_resume_upload_and_download(self):
        status, _, _ = self.request("POST", "/profile/resume", RESUME.encode(), {"X-Filename": "my%20cv.txt"})
        self.assertEqual(status, 200)
        status, headers, body = self.request("GET", "/profile/resume")
        self.assertEqual((status, body), (200, RESUME.encode()))
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        self.assertIn("my cv.txt", self.request("GET", "/profile")[2].decode())
        status, _, body = self.request("POST", "/profile/resume", b"MZ", {"X-Filename": "virus.exe"})
        self.assertEqual((status, body.decode()), (400, "Upload a PDF, DOCX or TXT file."))

    def test_parse_route_fills_empty_fields(self):
        self.request("POST", "/profile/resume", RESUME.encode(), {"X-Filename": "cv.txt"})
        fake = ai.Provider("fake", "http://x", "m", "k")
        with mock.patch.object(ai, "providers", return_value=[fake]), \
                mock.patch.object(ai, "chat", return_value=json.dumps(LLM_FACTS)):
            status, _, body = self.request("POST", "/profile/parse")
        self.assertEqual(status, 200)
        self.assertGreater(int(body), 3)
        prof = self.stored()[0]
        self.assertEqual(prof["last_name"], "Rao")
        self.assertEqual(prof["facts_model"], "fake/m")


if __name__ == "__main__":
    unittest.main()
