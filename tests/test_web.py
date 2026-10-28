"""Dashboard filters, facet counts and sorting."""
import re
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from justjob import pipeline, web
from justjob.db import DB
from justjob.models import Job

from .helpers import make_cfg


def days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat(timespec="seconds")


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg(onsite=["india", "bengaluru"])
        self.db = DB(":memory:")
        jobs = [
            Job("greenhouse", "1", "AI Engineer", "Fresh Co", "https://x/1", "Remote", True,
                "2+ years with Python, PyTorch, LLMs", days_ago(1), "", "greenhouse:a"),
            Job("ashby", "2", "Backend Engineer", "Week Co", "https://x/2", "Bengaluru, India", False,
                "3+ years with Python", days_ago(6), "INR 20L", "ashby:b"),
            Job("greenhouse", "3", "Software Engineer", "Old Co", "https://x/3", "Remote", True,
                "Build things", days_ago(60), "", "greenhouse:a"),
        ]
        self.db.insert_jobs(jobs, days_ago(0))
        pipeline.apply_rules(self.cfg, self.db)
        pipeline.dedup(self.db)

    def tearDown(self):
        self.db.close()

    def page(self, **kw):
        return web.dashboard_html(self.db, self.cfg, web.Filters(**kw))

    def companies(self, **kw):
        return re.findall(r'data-company="([^"]+)"', self.page(**kw))

    def facet_count(self, page, name, value):
        m = re.search(rf'name="{name}" value="{value}"[^>]*><span>[^<]*</span><span class="n">([\d,]+)</span>', page)
        return int(m.group(1).replace(",", ""))

    def test_posted_filter_and_sort(self):
        self.assertEqual(len(self.companies()), 3)
        self.assertEqual(self.companies(posted="3"), ["Fresh Co"])
        self.assertEqual(self.companies(sort="newest"), ["Fresh Co", "Week Co", "Old Co"])

    def test_role_mode_experience_salary_and_search(self):
        self.assertEqual(self.companies(roles=("ai",)), ["Fresh Co"])
        self.assertEqual(self.companies(roles=("backend", "software"), sort="newest"), ["Week Co", "Old Co"])
        self.assertEqual(self.companies(modes=("onsite",)), ["Week Co"])
        self.assertEqual(self.companies(exp="2"), ["Fresh Co"])
        self.assertEqual(self.companies(exp="none"), ["Old Co"])
        self.assertEqual(self.companies(salary=True), ["Week Co"])
        self.assertEqual(self.companies(q="pytorch"), ["Fresh Co"])
        self.assertEqual(self.companies(q="python bengaluru"), ["Week Co"])
        self.assertEqual(self.companies(sources=("ashby",)), ["Week Co"])

    def test_facet_counts_ignore_their_own_filter(self):
        page = self.page(roles=("ai",))
        self.assertEqual(self.facet_count(page, "role", "ai"), 1)
        self.assertEqual(self.facet_count(page, "role", "backend"), 1)       # what ticking it would add
        self.assertEqual(self.facet_count(page, "mode", "remote"), 1)        # other facets respect the role filter
        self.assertEqual(self.facet_count(page, "mode", "onsite"), 0)
        self.assertIn('<span class="count">1</span>', page)
        self.assertIn('Matches<span class="n">1</span>', page)

    def test_query_roundtrip_and_chips(self):
        f = web.Filters(roles=("ai", "data"), modes=("remote",), exp="3", posted="7", salary=True, q="llm", sort="newest",
                        page=3, per=25)
        back = web.Filters.from_query(parse_qs(urlparse(f.url()).query))
        self.assertEqual(back, f)
        chips = f.chips(self.cfg)
        self.assertEqual([label for label, _ in chips], ["“llm”", "AI / ML", "Data", "Remote", "Experience: up to 3 years",
                                                         "Last 7 days", "Shows salary"])
        self.assertNotIn("page=", chips[0][1])   # removing a filter goes back to page 1
        self.assertEqual(web.Filters.from_query({"role": ["bogus"], "exp": ["9"], "sort": ["x"], "page": ["0"],
                                                 "per": ["7"]}), web.Filters())

    def test_pagination(self):
        extra = [Job("lever", str(i), f"Backend Engineer {i}", f"Co {i:02d}", f"https://y/{i}", "Remote", True,
                     "2+ years of Python", days_ago(2), "", "lever:c") for i in range(30)]
        self.db.insert_jobs(extra, days_ago(0))
        pipeline.apply_rules(self.cfg, self.db)
        pipeline.dedup(self.db)
        first, second = self.page(per=25), self.page(per=25, page=2)
        self.assertEqual((len(re.findall("<article", first)), len(re.findall("<article", second))), (25, 8))
        self.assertIn("Page 2 of 2", second)
        self.assertIn("Showing 26–33 of 33", second)
        self.assertIn('rel="prev"', second)
        self.assertNotIn('rel="next"', second)
        self.assertEqual(self.companies(per=25, page=99), self.companies(per=25, page=2))   # past the end: last page

    def test_page_numbers(self):
        self.assertEqual(web.page_numbers(1, 1), [1])
        self.assertEqual(web.page_numbers(6, 105), [1, None, 4, 5, 6, 7, 8, None, 105])
        self.assertEqual(web.page_numbers(2, 5), [1, 2, 3, 4, 5])

    def test_rows_show_dates_and_skills(self):
        page = self.page()
        self.assertIn('class="posted fresh" title="Posted', page)
        self.assertIn('<span class="skill">python</span>', page)
        self.assertIn("Experience not stated", page)

    def test_role_groups(self):
        for title, role in [("Senior LLM Engineer", "ai"), ("Full-Stack Developer", "fullstack"),
                            ("React Developer", "frontend"), ("Platform Engineer", "backend"),
                            ("Data Engineer", "data"), ("Software Engineer II", "software")]:
            self.assertEqual(web.role_of(title), role, title)


if __name__ == "__main__":
    unittest.main()
