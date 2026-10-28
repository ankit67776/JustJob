"""Source parsers against canned API responses (no network)."""
import unittest
from unittest import mock

from justjob.sources import jsearch, source_label

JSEARCH_V2 = {
    "status": "OK",
    "data": {
        "cursor": "abc",
        "jobs": [{
            "job_id": "R2Vz", "job_title": "AI Engineers", "employer_name": "CrewLake",
            "job_publisher": "LinkedIn",
            "job_apply_link": "https://in.linkedin.com/jobs/view/4479367578",
            "apply_options": [
                {"apply_link": "https://in.linkedin.com/jobs/view/4479367578", "is_direct": False, "publisher": "LinkedIn"},
                {"apply_link": "https://crewlake.com/careers/ai", "is_direct": True, "publisher": "CrewLake"},
            ],
            "job_description": "2+ years building LLM apps", "job_is_remote": False,
            "job_posted_at_datetime_utc": "2026-10-16T00:00:00.000Z",
            "job_location": "Bonth, Odisha", "job_city": "Bonth", "job_state": "Odisha", "job_country": "IN",
        }],
    },
}


class JSearchTests(unittest.TestCase):
    def test_v2_response(self):
        with mock.patch.object(jsearch, "get_json", return_value=JSEARCH_V2) as get:
            res = jsearch.fetch_query({"country": "in"}, "key", "AI engineer", None)
        self.assertTrue(get.call_args.args[0].endswith("/search-v2"))
        job = res.jobs[0]
        self.assertEqual(job.location, "Bonth, Odisha, India")
        self.assertEqual(job.url, "https://crewlake.com/careers/ai")   # direct link preferred
        self.assertEqual(job.posted_at, "2026-10-16T00:00:00+00:00")
        self.assertEqual(source_label(job.source, job.board), "LinkedIn (JSearch)")


class RemoteBoardTests(unittest.TestCase):
    def test_weworkremotely_rss(self):
        from justjob.sources import weworkremotely
        xml = b"""<?xml version="1.0"?><rss version="2.0"><channel><item>
          <title>Lithic: Software Engineer, Product Platform</title>
          <region>Anywhere in the World</region><country></country>
          <description>&lt;p&gt;2+ years of Python&lt;/p&gt;</description>
          <pubDate>Fri, 16 Oct 2026 14:00:00 +0000</pubDate>
          <link>https://weworkremotely.com/remote-jobs/lithic-software-engineer-product-platform</link>
        </item></channel></rss>"""
        job = weworkremotely.parse(xml)[0]
        self.assertEqual((job.company, job.title), ("Lithic", "Software Engineer, Product Platform"))
        self.assertEqual(job.location, "Remote - Anywhere in the World")
        self.assertEqual(job.description, "2+ years of Python")
        self.assertEqual(job.posted_at, "2026-10-16T14:00:00+00:00")

    def test_himalayas(self):
        from justjob.sources import himalayas
        job = himalayas.to_job({
            "title": "AI Engineer", "companyName": "Acme", "seniority": ["Mid-level"],
            "locationRestrictions": ["India", "Singapore"], "description": "<p>Build agents</p>",
            "pubDate": 1792331533, "applicationLink": "https://himalayas.app/companies/acme/jobs/ai-engineer",
            "guid": "https://himalayas.app/companies/acme/jobs/ai-engineer",
        })
        self.assertEqual(job.source_id, "acme/jobs/ai-engineer")
        self.assertEqual(job.location, "Remote - India, Singapore")
        self.assertTrue(job.description.startswith("Seniority: Mid-level"))

    def test_jobicy_and_workingnomads(self):
        from justjob.sources import jobicy, workingnomads
        j = jobicy.to_job({"id": 7, "url": "https://jobicy.com/jobs/7-x", "jobTitle": "Backend Engineer",
                           "companyName": "Acme", "jobGeo": "Anywhere", "jobLevel": "Midweight",
                           "jobDescription": "<p>Go</p>", "pubDate": "2026-10-18T08:58:23+00:00"})
        self.assertEqual(j.location, "Remote (anywhere)")
        w = workingnomads.to_job({"url": "https://www.workingnomads.com/job/go/1944774/", "title": "ML Engineer",
                                  "company_name": "Acme", "location": "USA only", "pub_date": "2026-10-15T14:12:21-04:00"})
        self.assertEqual((w.source_id, w.location), ("1944774", "Remote - USA only"))


class DiscoverTests(unittest.TestCase):
    def test_find_and_add(self):
        import tempfile
        from pathlib import Path
        from justjob import discover, registry
        from justjob.db import DB
        from justjob.models import Job
        from .helpers import make_cfg
        cfg = make_cfg()
        cfg["sources"] = {"greenhouse": {"boards": {"acme": "Acme"}}, "workable": {"boards": {}}}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(registry, "DIR", Path(d)):
            registry.save("ashby", {"listed-co": ("Listed Co", 4)})
            db = DB(":memory:")
            db.insert_jobs([Job("hackernews", "1", "Engineer", "X", "https://news.ycombinator.com/item?id=1",
                                description="Apply: https://apply.workable.com/newco/j/ABC and "
                                            "https://boards.greenhouse.io/acme/jobs/1 or jobs.lever.co/other-co/xyz "
                                            "https://jobs.ashbyhq.com/listed-co/1 "
                                            "https://bigcorp.wd5.myworkdayjobs.com/en-US/BigCorp_Careers/job/X_R1")],
                           "2026-01-01")
            found = {(c.ats, c.slug) for c in discover.find_links(db, cfg)}
            db.close()
            self.assertIn(("workable", "newco"), found)
            self.assertIn(("lever", "other-co"), found)
            self.assertIn(("workday", "bigcorp/wd5/bigcorp_careers"), found)
            self.assertNotIn(("greenhouse", "acme"), found)      # already in config.toml
            self.assertNotIn(("ashby", "listed-co"), found)      # already in companies/

            n = discover.add_to_registry([discover.Candidate("workable", "newco", 1, 3, "New Co")])
            self.assertEqual(n, 1)
            self.assertEqual(registry.load("workable"), {"newco": ("New Co", 3)})


if __name__ == "__main__":
    unittest.main()
