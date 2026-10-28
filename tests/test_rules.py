import unittest

from justjob.filters import Rules, year_mentions

from .helpers import make_cfg


class TitleTests(unittest.TestCase):
    rules = Rules(make_cfg())

    def test_passes(self):
        for t in ["Software Engineer", "Machine Learning Engineer", "AI Engineer, Applied",
                  "Backend Engineer (Python)", "Software Engineer II", "SDE 1", "Member of Technical Staff",
                  "New Grad Software Engineer", "Research Engineer, Inference", "Full Stack Developer",
                  "Data Scientist", "Forward Deployed Engineer", "Frontend Engineer",
                  # soft-excluded: stored, decided once the description is read
                  "Senior Software Engineer", "Sr. ML Engineer", "Software Engineer III"]:
            self.assertTrue(self.rules.title_ok(t)[0], t)

    def test_rejects(self):
        for t in ["Staff Engineer, Platform", "Engineering Manager", "Tech Lead, Infrastructure",
                  "Account Executive, AI Native", "Software Engineering Intern", "Solutions Engineer",
                  "Recruiter", "Principal Data Scientist", "Network Engineer", "Staff SWE", "iOS Engineer"]:
            self.assertFalse(self.rules.title_ok(t)[0], t)


class ExperienceTests(unittest.TestCase):
    rules = Rules(make_cfg())   # max_years_required = 4

    def test_year_mentions(self):
        self.assertEqual(year_mentions("3+ years of experience with Python"), [3])
        self.assertEqual(year_mentions("1–2 years of experience in software"), [1])
        self.assertEqual(year_mentions("1 to 3 years of experience in operations"), [1])
        self.assertEqual(year_mentions("At least three years of previous experience"), [3])
        self.assertEqual(year_mentions("Experience: 2 years"), [2])
        self.assertEqual(year_mentions("between 2 and 4 years of work"), [2])
        self.assertEqual(year_mentions("Founded 10 years ago, we have 5 offices"), [])
        self.assertEqual(year_mentions("A 4 year degree or equivalent experience"), [])
        self.assertEqual(year_mentions("Bachelor's degree and 2+ years of experience"), [2])

    def test_experience(self):
        e = self.rules.experience
        self.assertEqual(e("Software Engineer", "You have 5+ years of backend experience.")[0], "reject")
        self.assertEqual(e("Software Engineer", "4+ years of experience building APIs")[0], "pass")
        self.assertEqual(e("Software Engineer", "2+ years required, 6+ years preferred")[0], "unclear")
        self.assertEqual(e("Software Engineer", "We build models.")[0], "unclear")
        self.assertEqual(e("Junior Software Engineer", "We build models.")[0], "pass")
        self.assertEqual(e("Software Engineer", "Great for new grads and early-career engineers")[0], "pass")
        self.assertEqual(e("New Grad Engineer", "7+ years of experience")[0], "unclear")

    def test_soft_excluded_titles(self):
        c = self.rules.classify
        self.assertEqual(c("Senior Software Engineer", "Remote", True, "3+ years of Python experience").status, "pass")
        self.assertEqual(c("Senior Software Engineer", "Remote", True, "6+ years of experience").status, "reject")
        self.assertEqual(c("Senior Software Engineer", "Remote", True, "We move fast.").status, "reject")

    def test_score_never_zero_for_kept_jobs(self):
        r = self.rules.classify("Software Engineer", "Remote", True, "We build things.")
        self.assertEqual(r.status, "unclear")
        self.assertGreaterEqual(r.score, 10)
        ai = self.rules.classify("AI Engineer", "Remote", True, "2+ years with Python, PyTorch, LLMs, FastAPI")
        self.assertGreater(ai.score, r.score)
        self.assertEqual(ai.skills, ["python", "pytorch", "llm", "fastapi"])


class LocationTests(unittest.TestCase):
    def test_any(self):
        self.assertTrue(Rules(make_cfg()).location_ok("Tokyo, Japan", False)[0])

    def test_restricted(self):
        r = Rules(make_cfg(onsite=["bengaluru", "bangalore"], remote=True, remote_regions=["india", "apac"]))
        self.assertTrue(r.location_ok("Bengaluru, India", False)[0])
        self.assertFalse(r.location_ok("San Francisco, CA", False)[0])
        self.assertTrue(r.location_ok("Remote", True)[0])
        self.assertTrue(r.location_ok("Worldwide", True)[0])
        self.assertTrue(r.location_ok("Home based - Worldwide", None)[0])
        self.assertTrue(r.location_ok("Remote - India", True)[0])
        self.assertFalse(r.location_ok("Remote - US", True)[0])
        self.assertFalse(r.location_ok("USA, Canada, USA timezones", True)[0])
        self.assertTrue(r.location_ok("Remote (US) or Bangalore", True)[0])

    def test_india_onsite_all_remote(self):
        r = Rules(make_cfg(onsite=["india", "hyderabad"], remote=True, remote_regions=["*"]))
        self.assertTrue(r.location_ok("Remote - US", True)[0])
        self.assertTrue(r.location_ok("Hyderabad, TS, India", False)[0])
        self.assertFalse(r.location_ok("Indianapolis, IN", False)[0])
        self.assertFalse(r.location_ok("London, UK", False)[0])

    def test_no_remote(self):
        r = Rules(make_cfg(onsite=["london"], remote=False, remote_regions=["*"]))
        self.assertFalse(r.location_ok("Remote", True)[0])
        self.assertTrue(r.location_ok("London, UK", False)[0])


if __name__ == "__main__":
    unittest.main()
