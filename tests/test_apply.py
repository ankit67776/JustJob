"""The apply agent: reading forms, drafting answers, the review flow, and filling a local test form."""
import http.client
import json
import tempfile
import threading
import time
import unittest
import urllib.parse
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from justjob import ai, web
from justjob import profile as P
from justjob.apply import agent, browser, forms, memory
from justjob.apply import answers as A
from justjob.apply.answers import Answer
from justjob.apply.forms import Question
from justjob.db import DB
from justjob.models import Job

from .helpers import make_cfg

PROF = {
    "first_name": "Asha", "last_name": "Rao", "email": "asha@example.com", "phone": "+91 90000 00000",
    "location": "Bengaluru, India", "linkedin": "https://linkedin.com/in/asha", "github": "https://github.com/asha",
    "portfolio": "", "years_experience": 3.5, "notice_period_days": 30, "expected_ctc": "₹28 LPA",
    "current_ctc": "₹18 LPA", "expected_salary_usd": "", "authorized_countries": ["India"], "sponsorship_needed": "Yes",
    "relocation": "Yes", "gender": "Female", "ethnicity": P.DECLINE, "veteran": "I am not a protected veteran",
    "disability": P.DECLINE, "how_heard": "Company careers page", "over_18": "Yes", "current_company": "Acme",
    "current_title": "AI Engineer", "skills": ["Python", "FastAPI"], "summary": "I build LLM apps.",
    "resume": {"file": "resume.pdf", "name": "Asha.pdf"},
    "facts": {"experience": [{"title": "AI Engineer", "company": "Acme", "start": "2023-01", "end": "Present",
                              "internship": False, "highlights": ["Built a RAG assistant with FastAPI"]}],
              "education": [], "projects": [], "certifications": [], "skills": ["Python", "FastAPI", "PyTorch"]},
}
US_JOB = {"title": "AI Engineer", "company": "Oura", "location": "San Francisco, CA", "description": "Python", "salary": ""}
IN_JOB = {"title": "AI Engineer", "company": "Databricks", "location": "Bengaluru, India", "description": "", "salary": ""}

GH_DATA = {
    "questions": [
        {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text", "values": []}]},
        {"label": "Resume/CV", "required": True, "fields": [{"name": "resume", "type": "input_file", "values": []},
                                                           {"name": "resume_text", "type": "textarea", "values": []}]},
        {"label": "Are you legally authorized to work in the United States?", "required": True,
         "fields": [{"name": "question_1", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
        {"label": "Tools you use", "required": False,
         "fields": [{"name": "question_2[]", "type": "multi_value_multi_select",
                     "values": [{"label": "Python", "value": 1}, {"label": "Go", "value": 2}]}]},
        {"label": "Hidden", "required": False, "fields": [{"name": "x", "type": "input_hidden", "values": []}]},
    ],
    "location_questions": [{"label": "Location", "required": True, "fields": [{"name": "location", "type": "input_text"}]}],
    "compliance": [{"type": "eeoc", "questions": [{"label": "Gender", "required": False, "fields": [
        {"name": "gender", "type": "multi_value_single_select",
         "values": [{"label": "Decline To Self Identify", "value": "3"}, {"label": "Female", "value": "1"}]}]}]}],
}
ASHBY_DATA = {"data": {"jobPosting": {"applicationForm": {"sections": [{"fieldEntries": [
    {"field": {"path": "_systemfield_name", "title": "Name", "type": "String"}, "isRequired": True},
    {"field": {"path": "abc", "title": "Do you need a visa?", "type": "Boolean"}, "isRequired": True},
    {"field": {"path": "def", "title": "Pick one", "type": "ValueSelect",
               "selectableValues": [{"label": "A", "value": "a"}, {"label": "B", "value": "b"}]}, "isRequired": False},
    {"field": {"path": "ghi", "title": "Secret", "type": "String"}, "isRequired": False, "isHidden": True},
]}]}, "surveyForms": [{"sections": [{"fieldEntries": [
    {"field": {"path": "eeo1", "title": "Gender", "type": "ValueSelect",
               "selectableValues": [{"label": "Prefer not to say"}, {"label": "Woman"}]}, "isRequired": False}]}]}]}}}


def q(label, kind="text", required=True, options=None, qid="q", group="custom"):
    return Question(qid, label, kind, required, options or [], group)


class FormTests(unittest.TestCase):
    def test_greenhouse_schema(self):
        qs = {x.id: x for x in forms.greenhouse_questions(GH_DATA)}
        self.assertEqual(list(qs), ["first_name", "resume", "question_1", "question_2[]", "candidate-location", "gender"])
        self.assertEqual((qs["resume"].kind, qs["resume"].group), ("file", "standard"))
        self.assertEqual((qs["question_1"].kind, qs["question_1"].options), ("select", ["Yes", "No"]))
        self.assertEqual(qs["question_2[]"].kind, "multiselect")
        self.assertEqual((qs["candidate-location"].kind, qs["gender"].group), ("location", "eeo"))

    def test_ashby_schema(self):
        qs = {x.id: x for x in forms.ashby_questions(ASHBY_DATA)}
        self.assertEqual(list(qs), ["_systemfield_name", "abc", "def", "eeo1"])
        self.assertEqual((qs["abc"].kind, qs["abc"].options), ("boolean", ["Yes", "No"]))
        self.assertEqual((qs["_systemfield_name"].group, qs["eeo1"].group), ("standard", "eeo"))
        with self.assertRaises(forms.FormError):
            forms.ashby_questions({"data": {"jobPosting": None}})

    def test_target_from_ids_and_links(self):
        t = forms.target({"id": "greenhouse:oura/42", "url": "https://ouraring.com/careers?gh_jid=42"})
        self.assertEqual((t.ats, t.board, t.posting), ("greenhouse", "oura", "42"))
        self.assertEqual(t.apply_url, "https://job-boards.greenhouse.io/oura/jobs/42")
        uuid = "fc1fe232-e213-4a26-b9ec-aab365573476"
        t = forms.target({"id": "himalayas:9", "url": f"https://jobs.ashbyhq.com/Clera/{uuid}?src=x"})
        self.assertEqual(t.apply_url, f"https://jobs.ashbyhq.com/Clera/{uuid}/application")
        t = forms.target({"id": "remotive:1", "url": "https://boards.greenhouse.io/acme/jobs/77"})
        self.assertEqual((t.board, t.posting), ("acme", "77"))
        self.assertIsNone(forms.target({"id": "workday:x/1", "url": "https://acme.wd5.myworkdayjobs.com/x"}))


class MatchingTests(unittest.TestCase):
    def test_pick(self):
        decline = ["I don't wish to answer", "I am a protected veteran", "I am not a protected veteran"]
        self.assertEqual(A.pick(decline, P.DECLINE), "I don't wish to answer")
        self.assertEqual(A.pick(decline, "I am not a protected veteran"), "I am not a protected veteran")
        self.assertEqual(A.pick(["Yes, I have one", "No, I do not have a disability"], "No, I don't have a disability"),
                         "No, I do not have a disability")
        self.assertEqual(A.pick(["Man", "Woman"], "Female"), "Woman")
        self.assertEqual(A.pick(["Yes", "No"], "no"), "No")
        self.assertIsNone(A.pick(["Yes", "No"], "Maybe"))
        self.assertIsNone(A.pick(["I am not a protected veteran"], "no"))   # "no" is not "not"

    def test_pick_number_and_days(self):
        self.assertEqual(A.pick_number(["0-2 years", "3-5 years", "5+ years"], 3.5), "3-5 years")
        self.assertEqual(A.pick_number(["Less than 1 year", "1 to 3 years", "More than 3 years"], 0.5), "Less than 1 year")
        self.assertEqual(A.pick_number(["10 or more"], 12), "10 or more")
        self.assertEqual(A._pick_days(["Immediately", "Within 2 weeks", "1 month", "2 months"], 30), "1 month")
        self.assertEqual(A._pick_days(["Immediately", "1 month"], 0), "Immediately")

    def test_country_in(self):
        self.assertEqual(A.country_in("San Francisco, CA"), "United States")
        self.assertEqual(A.country_in("Remote - India"), "India")
        self.assertEqual(A.country_in("authorized to work in the U.S."), "United States")
        self.assertIsNone(A.country_in("Tell us about yourself"))
        self.assertIsNone(A.country_in("London or New York"))   # two countries: unclear


class RuleTests(unittest.TestCase):
    def ans(self, question, job=US_JOB, prof=PROF):
        a = A.rule_answer(question, prof, job)
        return a

    def test_profile_fields(self):
        self.assertEqual(self.ans(q("First Name", qid="first_name")).value, "Asha")
        self.assertEqual(self.ans(q("Full name", qid="_systemfield_name")).value, "Asha Rao")
        self.assertEqual(self.ans(q("LinkedIn Profile")).value, "https://linkedin.com/in/asha")
        self.assertEqual(self.ans(q("Website", required=False)).value, "https://github.com/asha")
        self.assertEqual(self.ans(q("City")).value, "Bengaluru")
        self.assertEqual(self.ans(q("Resume/CV", "file", qid="resume")).value, "resume")
        self.assertEqual(self.ans(q("Please share the name of your current firm.")).value, "Acme")
        self.assertEqual(self.ans(q("Years of experience", "select", options=["0-2", "3-5", "6+"])).value, "3-5")

    def test_authorization_follows_the_country(self):
        us = self.ans(q("Are you legally authorized to work in the United States?", "select", options=["Yes", "No"]))
        self.assertEqual((us.value, us.source, us.needs_you), ("No", "rule", False))
        that = q("Do you have work authorization to work in that country?", "boolean", options=["Yes", "No"])
        self.assertEqual(self.ans(that, IN_JOB).value, "Yes")
        self.assertTrue(self.ans(that, {**US_JOB, "location": "Remote"}).needs_you)

    def test_sponsorship(self):
        sponsor = q("Will you now or in the future require sponsorship?", "select", options=["Yes", "No"])
        self.assertEqual(self.ans(sponsor, IN_JOB).value, "No")
        self.assertEqual(self.ans(sponsor, US_JOB).value, "Yes")
        self.assertTrue(self.ans(sponsor, {**US_JOB, "location": "Remote"}).needs_you)
        self.assertTrue(self.ans(sponsor, prof={**PROF, "authorized_countries": []}).needs_you)

    def test_pay_depends_on_the_currency(self):
        self.assertEqual(self.ans(q("Expected CTC"), IN_JOB).value, "₹28 LPA")
        self.assertTrue(self.ans(q("What are your salary expectations?"), US_JOB).needs_you)   # no dollar figure set
        self.assertEqual(self.ans(q("Notice period")).value, "30 days")

    def test_voluntary_and_signed_questions(self):
        gender = q("Gender", "select", options=["Decline To Self Identify", "Female"], group="eeo")
        self.assertEqual(A.draft([gender], PROF, US_JOB, make_cfg(), DB(":memory:"))["q"].value, "Female")
        self.assertEqual(self.ans(q("Are you Hispanic/Latino?", "select", qid="hispanic_ethnicity")).value, P.DECLINE)
        sign = self.ans(q("I certify that the information above is true and complete", "select", options=["I agree"]))
        self.assertEqual((sign.value, sign.needs_you), ("I agree", True))

    def test_worked_here_uses_the_resume(self):
        self.assertEqual(self.ans(q("Have you previously worked for Acme?"), {**US_JOB, "company": "Acme"}).value, "Yes")
        self.assertEqual(self.ans(q("Have you ever been employed by Oura?")).value, "No")

    def test_optional_free_text_is_left_empty_and_unknowns_go_further(self):
        self.assertEqual(self.ans(q("Anything else?", "textarea", required=False)).source, "none")
        self.assertIsNone(self.ans(q("Why do you want to work here?", "textarea")))
        self.assertIsNone(self.ans(q("Years of experience with Kubernetes", "select", options=["0-1", "2+"])))


class LLMTests(unittest.TestCase):
    def run_llm(self, questions, reply):
        fake = ai.Provider("fake", "http://x", "m", "k")
        with mock.patch.object(ai, "providers", return_value=[fake]), \
                mock.patch.object(ai, "chat", return_value=json.dumps(reply)) as chat:
            out = A.draft(questions, PROF, US_JOB, make_cfg(), DB(":memory:"))
        return out, chat

    def test_answers_are_checked_against_options_and_facts(self):
        qs = [q("Experience with FastAPI?", "select", options=["Yes", "No"], qid="a"),
              q("Experience with Rust?", "select", options=["Yes", "No"], qid="b"),
              q("Favourite colour", "select", options=["Red", "Blue"], qid="c"),
              q("Why us?", "textarea", qid="d"),
              q("Clearance level", "text", qid="e")]
        reply = {"answers": [
            {"id": "q1", "value": "yes", "confidence": "high", "fact": "Built a RAG assistant with FastAPI"},
            {"id": "q2", "value": "Yes", "confidence": "high", "fact": "Ten years of Rust at Mozilla"},
            {"id": "q3", "value": "Green", "confidence": "high", "fact": None},
            {"id": "q4", "value": "I like what you build.", "confidence": "medium", "fact": None},
            {"id": "q5", "value": None, "needs_human": True}]}
        out, chat = self.run_llm(qs, reply)
        self.assertEqual((out["a"].value, out["a"].source, out["a"].needs_you), ("Yes", "ai", False))
        self.assertTrue(out["b"].needs_you)                          # quoted "fact" isn't in the profile
        self.assertTrue(out["c"].needs_you and out["c"].empty)        # not an option
        self.assertTrue(out["d"].needs_you and out["d"].value)        # drafts always go to you
        self.assertTrue(out["e"].needs_you and out["e"].empty)
        prompt = chat.call_args[0][1][1]["content"]
        self.assertIn("Built a RAG assistant with FastAPI", prompt)
        self.assertIn("q1 | select | required | Experience with FastAPI?", prompt)

    def test_without_a_key_unknowns_go_to_you(self):
        out = A.draft([q("Why us?", "textarea")], PROF, US_JOB, make_cfg(), DB(":memory:"))
        self.assertTrue(out["q"].needs_you)
        self.assertIn("No AI key", out["q"].note)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.cfg = make_cfg()
        self.db.insert_jobs([Job("greenhouse", "oura/42", "AI Engineer", "Oura", "https://x/42", "San Francisco, CA",
                                 False, "Python", None, "", "greenhouse:oura")], "2026-01-01T00:00:00+00:00")
        self.db.save_profile(PROF, "2026-01-01T00:00:00+00:00")
        self.questions = [q("Are you legally authorized to work in the United States?", "select", options=["Yes", "No"],
                            qid="auth"),
                          q("Preferred stack", "select", options=["Python", "Go"], qid="stack"),
                          q("First Name", qid="first_name", group="standard")]

    def tearDown(self):
        self.db.close()

    def draft(self):
        with mock.patch.object(agent, "fetch_questions", return_value=self.questions):
            return agent.draft(self.db, self.cfg, "greenhouse:oura/42")

    def test_draft_and_approve(self):
        app = self.draft()
        self.assertEqual(app["state"], "drafted")
        _, answers = agent.parts(app)
        self.assertEqual(answers["auth"].value, "No")
        self.assertTrue(answers["stack"].needs_you)                   # no AI key in tests
        agent.approve(self.db, "greenhouse:oura/42", {"stack": ["Python"], "first_name": ["Asha"], "auth": ["Maybe"]})
        _, answers = agent.parts(self.db.get_application("greenhouse:oura/42"))
        self.assertEqual((answers["stack"].value, answers["stack"].source), ("Python", "you"))
        self.assertEqual(answers["first_name"].source, "profile")       # unchanged
        self.assertEqual(answers["auth"].value, "No")                   # "Maybe" isn't an option
        self.assertEqual(self.db.memory_all(), [])                      # learning waits for a submitted form

    def submit(self, final, answers=None):
        app = self.db.get_application("greenhouse:oura/42")
        questions, drafted = agent.parts(app)
        job = agent.job_row(self.db, "greenhouse:oura/42")
        return memory.learn(self.db, job, questions, answers or drafted, final, "2026-01-02T00:00:00+00:00")

    def test_learns_from_submitted_forms_and_trusts_after_two(self):
        self.draft()
        out = self.submit({"auth": ["No"], "stack": ["Python"], "first_name": ["Asha"]})
        self.assertEqual((out["kept"], out["added"], out["changed"], out["saved"]), (2, 1, 0, ["Preferred stack"]))
        _, answers = agent.parts(self.draft())                          # the next form asks again
        stack = answers["stack"]
        self.assertEqual((stack.value, stack.source, stack.needs_you), ("Python", "memory", True))
        self.assertIn("Keep it once more", stack.note)
        self.submit({"stack": ["Python"]})                              # kept it: trusted from now on
        _, answers = agent.parts(self.draft())
        self.assertFalse(answers["stack"].needs_you)
        out = self.submit({"stack": ["Go"]})                            # changed it: back to checking
        self.assertEqual(out["changed"], 1)
        _, answers = agent.parts(self.draft())
        self.assertEqual((answers["stack"].value, answers["stack"].needs_you), ("Go", True))

    def test_warnings(self):
        app = self.draft()
        questions, answers = agent.parts(app)
        job = agent.job_row(self.db, "greenhouse:oura/42")
        warns = agent.warnings(self.db, self.cfg, job, questions, answers, P.load(self.db, self.cfg))
        self.assertTrue(any("you'll answer No" in w for w in warns))

    def test_daily_cap(self):
        cfg = {**self.cfg, "apply": {"max_per_day": 1}}
        self.assertIsNone(agent.over_cap(self.db, cfg))
        self.draft()
        self.db.update_application("greenhouse:oura/42", "now", submitted_at=agent.start_of_today())
        self.assertIn("daily limit", agent.over_cap(self.db, cfg))

    def test_unsupported_jobs_are_refused(self):
        self.db.insert_jobs([Job("workday", "acme/1", "AI Engineer", "Acme", "https://acme.wd5.myworkdayjobs.com/1",
                                 "", None, "", None, "", "workday:acme")], "2026-01-01T00:00:00+00:00")
        with self.assertRaises(forms.FormError):
            agent.draft(self.db, self.cfg, "workday:acme/1")


NOW = "2026-01-02T00:00:00+00:00"


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.cfg = make_cfg()

    def tearDown(self):
        self.db.close()

    def save(self, label, value, kind="select", options=("Yes", "No"), confirmed=1, company="Coinbase", overrides=False):
        k = memory.key(q(label, kind, options=list(options)))
        self.db.memory_save(k, label, kind, list(options), value, confirmed, overrides, company, "x", NOW)

    def recall(self, question, job=IN_JOB):
        return memory.recall(question, self.db.memory_all(), self.cfg, job)

    def test_what_is_learnable(self):
        self.assertTrue(memory.learnable(q("Are you a government official?", "select", options=["Yes", "No"])))
        self.assertTrue(memory.learnable(q("Why us?", "textarea")))
        for not_saved in (q("Are you legally authorized to work in the US?", "select", options=["Yes", "No"]),
                          q("Do you need a visa?", "boolean", options=["Yes", "No"]),
                          q("I certify that the information above is true and complete", "select", options=["I agree"]),
                          q("Gender", "select", options=["Male"], group="eeo"),
                          q("First Name", qid="first_name", group="standard"),
                          q("Resume", "file")):
            self.assertFalse(memory.learnable(not_saved), not_saved.label)

    def test_same_question_worded_a_little_differently(self):
        self.save("Are you a current government official or were you one in the last five years?", "No", confirmed=5)
        close = self.recall(q("Are you a current government official, or were you one in the last 5 years?", "select",
                              options=["Yes", "No"]))
        self.assertEqual((close.value, close.source, close.needs_you), ("No", "memory", True))   # always checked
        self.assertTrue(close.ref.startswith("~"))
        self.assertIn("similar question", close.note)
        self.assertIsNone(self.recall(q("Do you have a driving licence?", "select", options=["Yes", "No"])))
        # A saved answer the new options can't take isn't offered.
        self.assertIsNone(self.recall(q("Are you a current government official or were you one in the last five years?",
                                        "select", options=["Never", "Currently"])))

    def test_written_answers_stay_drafts_and_flag_the_old_company(self):
        self.save("Why do you want to join us?", "I want to build Coinbase's AI tools.", "textarea", (), confirmed=9)
        a = self.recall(q("Why do you want to join us?", "textarea"), {**IN_JOB, "company": "Oura"})
        self.assertTrue(a.needs_you)
        self.assertIn("It names Coinbase", a.note)
        self.save("Preferred working hours", "IST, flexible", "text", (), confirmed=3)
        a = self.recall(q("Preferred working hours", "text"))
        self.assertEqual((a.value, a.needs_you), ("IST, flexible", False))   # short text you kept on 3 forms

    def test_correcting_a_profile_answer_makes_yours_win(self):
        heard = q("How did you hear about this job?", "select", options=["LinkedIn", "Job Board", "Company website"])
        job = {**IN_JOB, "id": "greenhouse:acme/1"}
        drafted = A.draft([heard], PROF, job, self.cfg, self.db)
        self.assertEqual((drafted["q"].value, drafted["q"].source), ("Company website", "profile"))
        out = memory.learn(self.db, job, [heard], drafted, {"q": ["LinkedIn"]}, NOW)
        self.assertEqual((out["changed"], out["saved"]), (1, [heard.label]))
        again = A.draft([heard], PROF, job, self.cfg, self.db)["q"]
        self.assertEqual((again.value, again.source), ("LinkedIn", "memory"))

    def test_authorization_answers_teach_the_profile_not_the_memory(self):
        self.db.save_profile({**PROF, "authorized_countries": []}, NOW)
        auth = q("Are you legally authorized to work in the country where this job is located?", "select",
                 options=["Yes", "No"])
        drafted = A.draft([auth], P.load(self.db, self.cfg), IN_JOB, self.cfg, self.db)
        self.assertTrue(drafted["q"].needs_you)                            # the profile lists no countries yet
        out = memory.learn(self.db, IN_JOB, [auth], drafted, {"q": ["Yes"]}, NOW)
        self.assertEqual(out["profile"], ["You can work in India without a visa."])
        self.assertEqual(self.db.get_profile()["authorized_countries"], ["India"])
        self.assertEqual(self.db.memory_all(), [])
        self.assertEqual(A.draft([auth], P.load(self.db, self.cfg), IN_JOB, self.cfg, self.db)["q"].value, "Yes")

    def test_read_back(self):
        sel = q("Pick", "select", options=["Yes, I am", "No, I am not"])
        self.assertEqual(memory.read_back(sel, ["No, I am not"]), "No, I am not")
        self.assertIs(memory.read_back(sel, ["Select..."]), memory.MISSING)   # not one of the options
        self.assertIs(memory.read_back(sel, None), memory.MISSING)            # field not on the page
        self.assertEqual(memory.read_back(sel, []), "")
        multi = q("Tools", "multiselect", options=["Python", "Go"])
        self.assertEqual(memory.read_back(multi, ["Go", "Python"]), ["Go", "Python"])
        self.assertEqual(memory.read_back(q("Phone", "phone"), [" +91 90000 00000 "]), "+91 90000 00000")

    def test_unreadable_and_empty_fields_teach_nothing(self):
        stack = q("Preferred stack", "select", options=["Python", "Go"])
        drafted = {"q": Answer("Go", "ai", True, "AI guess")}
        out = memory.learn(self.db, IN_JOB, [stack], drafted, {}, NOW)
        self.assertEqual((out["fields"], out["saved"]), (0, []))
        out = memory.learn(self.db, IN_JOB, [stack], drafted, {"q": []}, NOW)
        self.assertEqual(out["fields"], 0)
        # An edit made on the review page counts when the browser couldn't read the field.
        out = memory.learn(self.db, IN_JOB, [stack], {"q": Answer("Python", "you", False, "You edited this.")}, {}, NOW)
        self.assertEqual((out["changed"], out["saved"]), (1, ["Preferred stack"]))

    def test_llm_may_reuse_a_saved_answer_only_as_saved(self):
        self.save("Are you a government official?", "No", confirmed=4)
        qs = [q("Do you hold public office?", "select", options=["Yes", "No"], qid="a"),
              q("Do you work in a civil service job?", "select", options=["Yes", "No"], qid="b")]
        reply = {"answers": [{"id": "q1", "value": "No", "past": "p1"},
                             {"id": "q2", "value": "Yes", "past": "p1", "confidence": "high"}]}
        fake = ai.Provider("fake", "http://x", "m", "k")
        with mock.patch.object(ai, "providers", return_value=[fake]), \
                mock.patch.object(ai, "chat", return_value=json.dumps(reply)) as chat:
            out = A.draft(qs, PROF, US_JOB, self.cfg, self.db)
        self.assertEqual((out["a"].value, out["a"].source, out["a"].needs_you), ("No", "memory", True))
        self.assertIn("means the same", out["a"].note)
        self.assertEqual((out["b"].source, out["b"].needs_you), ("ai", True))   # "Yes" isn't what you said
        self.assertIn('p1 | Are you a government official? | "No"', chat.call_args[0][1][1]["content"])


class AnswersPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "jobs.db"
        db = DB(self.db_path)
        db.memory_save("k", "Are you a government official?", "select", ["Yes", "No"], "No", 1, False, "Coinbase", "x", NOW)
        db.insert_jobs([Job("greenhouse", "acme/1", "AI Engineer", "Acme", "https://x/1", "", None, "", None, "",
                            "greenhouse:acme")], NOW)
        db.save_application({"job_id": "greenhouse:acme/1", "ats": "greenhouse", "apply_url": "x", "state": "submitted",
                             "questions": [], "answers": {}, "created_at": NOW, "updated_at": NOW, "submitted_at": NOW,
                             "report": {"outcome": {"kept": 9, "changed": 2, "added": 1, "fields": 12, "saved": ["a"]}}})
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

    def row(self):
        db = DB(self.db_path)
        try:
            return db.memory_get("k")
        finally:
            db.close()

    def test_page_lists_answers_and_the_record(self):
        status, _, page = self.request("GET", "/answers")
        self.assertEqual(status, 200)
        self.assertIn('aria-current="page">Answers', page)
        self.assertIn("Are you a government official?", page)
        self.assertIn("Asks you to check it once more", page)
        self.assertIn("9 of 12 (75%)", page)

    def test_edit_and_delete(self):
        self.assertEqual(self.request("POST", "/answers/save", {"key": "k", "value": "Maybe"})[1], "/answers?done=invalid")
        self.assertEqual(self.row()["value"], "No")
        self.assertEqual(self.request("POST", "/answers/save", {"key": "k", "value": "Yes"})[1], "/answers?done=saved")
        row = self.row()
        self.assertEqual((row["value"], row["confirmed"]), ("Yes", 2))   # your choice there is trusted
        self.assertEqual(self.request("POST", "/answers/delete", {"key": "k"})[1], "/answers?done=deleted")
        self.assertIsNone(self.row())


# --- the browser step, against a local page that behaves like a Greenhouse form ------------

TEST_FORM = """<!doctype html><html><body>
<form id="f" onsubmit="event.preventDefault(); done();">
  <label for="first_name">First Name</label><input id="first_name">
  <label for="email">Email</label><input id="email">
  <label for="resume">Resume</label><input id="resume" type="file">
  <label for="question_1">Authorized?</label>
  <div class="field"><input id="question_1" role="combobox" aria-controls="lb1" oninput="openList(this)">
    <span id="question_1_value" class="select__single-value"></span>
    <ul id="lb1" role="listbox" hidden><li role="option">Yes</li><li role="option">No</li></ul></div>
  <ul role="listbox" hidden><li role="option">Afghanistan</li><li role="option">No decoy</li></ul>
  <div class="field" id="vis"><input type="checkbox" name="visa" hidden>
    <button type="button" aria-pressed="false" onclick="press(this)">Yes</button>
    <button type="button" aria-pressed="false" onclick="press(this)">No</button></div>
  <div data-field-path="stack"><fieldset><label>Preferred stack</label>
    <label><input type="radio" name="x_stack" value="py">Python</label><label><input type="radio" name="x_stack" value="go">Go</label>
  </fieldset></div>
  <label for="essay">Why us?</label><textarea id="essay"></textarea>
  <button type="submit">Submit application</button>
</form>
<script>
function openList(input) {
  const lb = document.getElementById(input.getAttribute('aria-controls'));
  lb.hidden = false;
  lb.querySelectorAll('[role=option]').forEach(o => {
    o.hidden = !o.textContent.toLowerCase().includes(input.value.toLowerCase());
    o.onclick = () => { document.getElementById(input.id + '_value').textContent = o.textContent; lb.hidden = true; };
  });
}
function press(b) {
  b.parentNode.dataset.v = b.textContent;
  b.parentNode.querySelectorAll('button').forEach(x => x.setAttribute('aria-pressed', String(x === b)));
}
function done() { document.body.innerHTML = '<h1>Thank you for applying!</h1>'; }
// "You" press Submit once the agent has filled the last field: the click goes through the page's own button.
if (location.search.includes('autosubmit')) setTimeout(() => {
  const iv = setInterval(() => { if (document.getElementById('essay')?.value) { clearInterval(iv);
    setTimeout(() => document.querySelector('button[type=submit]').click(), 1500); } }, 300);
}, 100);
</script></body></html>"""


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _playwright_ok() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch(channel="msedge", headless=True).close()
        return True
    except Exception:
        return False


@unittest.skipUnless(_playwright_ok(), "needs Playwright and Microsoft Edge")
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "form.html").write_text(TEST_FORM, encoding="utf-8")
        cls.resume = root / "resume.txt"
        cls.resume.write_text("resume")
        handler = partial(QuietHandler, directory=str(root))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/form.html"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    QUESTIONS = [Question("first_name", "First Name", "text", True), Question("email", "Email", "email", True),
                 Question("resume", "Resume", "file", True), Question("question_1", "Authorized?", "select", True, ["Yes", "No"]),
                 Question("visa", "Visa?", "boolean", True, ["Yes", "No"]),
                 Question("stack", "Preferred stack", "select", True, ["Python", "Go"]),
                 Question("essay", "Why us?", "textarea", True)]
    ANSWERS = {"first_name": Answer("Asha", "profile"), "email": Answer("asha@example.com", "profile"),
               "resume": Answer("resume", "profile"), "question_1": Answer("No", "rule"), "visa": Answer("No", "rule"),
               "stack": Answer(None, "none", True, "No AI key"), "essay": Answer("Because.", "ai", True, "AI draft")}

    def test_fill_read_back_and_detect_submission(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(channel="msedge", headless=True)
            page = b.new_page()
            page.goto(self.url)
            f = browser.GreenhouseFiller(page, PROF, self.resume)
            report = f.fill_all(self.QUESTIONS, self.ANSWERS)
            self.assertEqual(len(report["filled"]), 6)
            self.assertEqual([t["id"] for t in report["todo"]], ["stack", "essay"])   # nobody's answer; an AI draft
            self.assertEqual(page.inner_text("#question_1_value"), "No")       # the right list, not the decoy
            self.assertEqual(page.get_attribute("#vis", "data-v"), "No")
            f.highlight(report)
            self.assertIn("filled 6 fields", page.inner_text("#justjob-banner"))
            f.watch(self.QUESTIONS)
            page.check("input[value=go]")                                    # you answer the one it couldn't
            page.fill("#essay", "Because I like it.")                       # and rewrite its draft
            self.assertFalse(f.confirmed())
            page.click("button[type=submit]")                               # read once more on the click
            self.assertEqual(f.wait_for_submit(0.2), "submitted")
            b.close()
        got = {q.id: memory.read_back(q, f.final.get(q.id)) for q in self.QUESTIONS if q.kind != "file"}
        self.assertEqual(got, {"first_name": "Asha", "email": "asha@example.com", "question_1": "No", "visa": "No",
                               "stack": "Go", "essay": "Because I like it."})

    def test_full_session_marks_the_job_applied(self):
        @dataclass(frozen=True)
        class LocalTarget(forms.Target):
            url: str = ""

            @property
            def apply_url(self):
                return self.url

        data = Path(self.tmp.name) / "data"
        db_path = str(data / "jobs.db")
        db = DB(db_path)
        db.insert_jobs([Job("greenhouse", "acme/1", "AI Engineer", "Acme", self.url, "", None, "", None, "",
                            "greenhouse:acme")], "2026-01-01T00:00:00+00:00")
        db.save_application({"job_id": "greenhouse:acme/1", "ats": "greenhouse", "apply_url": self.url, "state": "drafted",
                             "questions": [], "answers": {}, "created_at": "x", "updated_at": "x"})
        db.close()
        err = browser.start("greenhouse:acme/1", LocalTarget("greenhouse", "acme", "1", self.url + "?autosubmit=1"),
                            self.QUESTIONS, self.ANSWERS, prof=PROF, resume=self.resume, db_path=db_path,
                            data_dir=data, wait_minutes=0.5, headless=True)
        self.assertIsNone(err)
        self.assertIn("Another application", browser.start("x", None, [], {}, prof={}, resume=None, db_path=db_path,
                                                           data_dir=data))
        deadline = time.monotonic() + 60
        while browser.busy() and time.monotonic() < deadline:
            time.sleep(0.3)
        db = DB(db_path)
        app = db.get_application("greenhouse:acme/1")
        status = db.q("SELECT status FROM jobs WHERE id = 'greenhouse:acme/1'")[0][0]
        db.close()
        self.assertEqual((app["state"], status), ("submitted", "applied"), app.get("error"))
        self.assertTrue((data / "applications" / app["screenshot"]).exists())
        self.assertEqual(browser.LIVE["greenhouse:acme/1"]["state"], "submitted")
        # What went in was read back: the agent's draft of the essay was kept, so it's saved for next time.
        out = app["report"]["outcome"]
        self.assertEqual((out["kept"], out["changed"], out["added"], out["saved"]), (5, 0, 0, ["Why us?"]))
        self.assertIn("Saved 1 answer for next time", browser.LIVE["greenhouse:acme/1"]["message"])


if __name__ == "__main__":
    unittest.main()
