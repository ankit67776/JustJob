"""Workday, Teamtailor, Rippling and Gem parsers against canned API responses (no network)."""
import unittest
from datetime import datetime, timezone
from unittest import mock

from justjob.sources import FetchContext, gem, rippling, teamtailor, workday

NOW = datetime(2026, 10, 20, 15, 30, tzinfo=timezone.utc)


class WorkdayTests(unittest.TestCase):
    SLUG = "acme/wd5/acme_careers"

    def test_slug_from_links(self):
        for url, slug in [
            ("https://acme.wd5.myworkdayjobs.com/en-US/Acme_Careers/job/X_R1", "acme/wd5/acme_careers"),
            ("https://acme.wd1.myworkdayjobs.com/External", "acme/wd1/external"),
            ("https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs", ""),
        ]:
            self.assertEqual(workday.slug_from(workday.LINK.search(url)), slug, url)

    def test_search_query(self):
        self.assertEqual(workday.search_query(("engineer", "member of technical staff")),
                         'engineer OR "member of technical staff"')

    def test_posted_on(self):
        self.assertEqual(workday.posted_on("Posted Today", NOW), "2026-10-20T00:00:00+00:00")
        self.assertEqual(workday.posted_on("Posted Yesterday", NOW), "2026-10-19T00:00:00+00:00")
        self.assertEqual(workday.posted_on("Posted 30+ Days Ago", NOW), "2026-09-20T00:00:00+00:00")
        self.assertIsNone(workday.posted_on("", NOW))

    def test_listing_pages_and_partial_flag(self):
        def page(i):
            return {"title": f"Job {i}", "externalPath": f"/job/City/Job-{i}_R{i}", "locationsText": "Pune, India",
                    "postedOn": "Posted Today"}
        pages = [{"total": 45, "jobPostings": [page(i) for i in range(20)]},
                 {"total": 0, "jobPostings": [page(i) for i in range(20, 40)]},   # later pages report total 0
                 {"total": 0, "jobPostings": [page(i) for i in range(40, 45)]}]
        with mock.patch.object(workday, "post_json", side_effect=pages):
            posts, complete = workday._listing(self.SLUG)
        self.assertEqual((len(posts), complete), (45, True))

        with mock.patch.object(workday, "MAX_POSTINGS", 20), \
                mock.patch.object(workday, "post_json", side_effect=pages):
            posts, complete = workday._listing(self.SLUG)
        self.assertEqual((len(posts), complete), (20, False))

    def test_board_with_details(self):
        listing = {"total": 2, "jobPostings": [
            {"title": "AI Engineer", "externalPath": "/job/Pune/AI-Engineer_R100", "locationsText": "2 Locations",
             "postedOn": "Posted 3 Days Ago"},
            {"title": "Accountant", "externalPath": "/job/Pune/Accountant_R101", "locationsText": "Pune"},
        ]}
        detail = {"jobPostingInfo": {
            "jobDescription": "<p>2+ years of Python</p>", "location": "Pune, India",
            "additionalLocations": ["Remote, India"], "startDate": "2026-10-17",
            "externalUrl": "https://acme.wd5.myworkdayjobs.com/Acme_Careers/job/Pune/AI-Engineer_R100"}}
        ctx = FetchContext(wants=lambda job: "Engineer" in job.title)
        with mock.patch.object(workday, "post_json", return_value=listing), \
                mock.patch.object(workday, "get_json", return_value=detail) as get:
            res = workday.fetch_board(self.SLUG, "Acme", ctx)
        self.assertTrue(get.call_args.args[0].endswith("/wday/cxs/acme/acme_careers/job/Pune/AI-Engineer_R100"))
        self.assertTrue(res.snapshot)
        self.assertEqual(len(res.listed_ids), 2)
        job = res.jobs[0]
        self.assertEqual(job.id, "workday:acme/acme_careers/AI-Engineer_R100")
        self.assertEqual(job.board, "workday:acme/wd5/acme_careers")
        self.assertEqual(job.location, "Pune, India; Remote, India")
        self.assertTrue(job.remote)
        self.assertEqual(job.description, "2+ years of Python")
        self.assertEqual(job.posted_at, "2026-10-17T00:00:00+00:00")
        self.assertIn("Acme_Careers", job.url)


class SmallAtsTests(unittest.TestCase):
    def test_teamtailor(self):
        job = teamtailor.to_job("acme", "Acme", {
            "id": "uuid-1", "title": "Backend Developer", "url": "https://acme.teamtailor.com/jobs/77-backend",
            "date_published": "2026-10-01T00:00:00+02:00", "content_html": "<p>Node.js, 3 years</p>",
            "_jobposting": {"identifier": {"value": 77}, "jobLocationType": "TELECOMMUTE",
                            "jobLocation": [{"address": {"addressLocality": "Berlin", "addressCountry": "DE"}}]},
        })
        self.assertEqual((job.source_id, job.location, job.remote), ("acme/77", "Remote - Berlin, DE", True))
        self.assertEqual(job.posted_at, "2026-09-30T22:00:00+00:00")
        self.assertEqual(job.description, "Node.js, 3 years")

    def test_rippling(self):
        job = rippling.to_job("acme", "Acme", {"uuid": "u1", "name": " ML  Engineer ", "url": "https://ats.rippling.com/acme/jobs/u1",
                                               "workLocation": {"label": "Remote (United States)"}})
        self.assertEqual((job.title, job.remote), ("ML Engineer", True))
        rippling.fill_details(job, {"description": {"company": "About us", "role": "<p>Build models</p>"},
                                    "workLocations": ["Remote (United States)", "Remote (Canada)"],
                                    "createdOn": "2026-10-06T16:36:07.947000-07:00"})
        self.assertEqual(job.description, "Build models")
        self.assertEqual(job.location, "Remote (United States); Remote (Canada)")
        self.assertEqual(job.posted_at, "2026-10-06T23:36:07+00:00")

    def test_gem(self):
        job = gem.to_job("acme", "Acme", {
            "id": "9", "title": "Software Engineer", "absolute_url": "https://jobs.gem.com/acme/9",
            "content_plain": "3+ years", "location": {"name": "San Francisco, United States"},
            "location_type": "hybrid", "first_published_at": "2026-10-01T10:00:00.000Z",
        })
        self.assertEqual((job.source_id, job.remote, job.description), ("acme/9", False, "3+ years"))


if __name__ == "__main__":
    unittest.main()
