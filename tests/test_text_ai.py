import unittest

from justjob.ai import parse_results
from justjob.text import dedup_key, html_to_text, to_iso, trim_for_ai


class TextTests(unittest.TestCase):
    def test_html_to_text(self):
        self.assertEqual(html_to_text("<p>Hello&nbsp;<b>world</b></p><ul><li>One</li><li>Two</li></ul>"),
                         "Hello world\n\n• One\n\n• Two")
        self.assertEqual(html_to_text("&lt;p&gt;Escaped &amp;amp; twice&lt;/p&gt;"), "Escaped & twice")

    def test_dedup_key(self):
        self.assertEqual(dedup_key("Scale AI, Inc.", "Software Engineer (Remote)"),
                         dedup_key("Scale", "Software Engineer"))
        self.assertNotEqual(dedup_key("Acme", "Software Engineer"), dedup_key("Acme", "Software Engineer II"))

    def test_to_iso(self):
        self.assertEqual(to_iso(1786469891368), "2026-08-11T17:38:11+00:00")
        self.assertEqual(to_iso("2026-10-14T20:33:44"), "2026-10-14T20:33:44+00:00")
        self.assertIsNone(to_iso("yesterday"))

    def test_trim_drops_boilerplate(self):
        text = "Build ML systems.\nWe offer great dental and 401k.\nYou have 2+ years of Python experience."
        out = trim_for_ai(text, 1000)
        self.assertNotIn("dental", out)
        self.assertIn("2+ years", out)

    def test_trim_prefers_requirements(self):
        text = "\n".join(["Fluffy company story line number %d." % i for i in range(50)]
                         + ["Requirements: 2+ years of experience with PyTorch."])
        self.assertIn("PyTorch", trim_for_ai(text, 200))


class ParseTests(unittest.TestCase):
    def test_parse_fenced_and_coerced(self):
        text = """```json
{"results": [
 {"job_id": 1, "min_years_required": "2", "is_within_limit": "true", "match_score": 87.4,
  "reason": "Strong LLM fit", "flags": "contract"},
 {"job_id": "2", "min_years_required": null, "match_score": 40, "reason": "Frontend heavy", "flags": []},
 {"job_id": "99", "match_score": 10}
]}
```"""
        out = parse_results(text, {"1", "2"}, 3)
        self.assertEqual(out["1"]["match_score"], 87)
        self.assertEqual(out["1"]["min_years_required"], 2.0)
        self.assertTrue(out["1"]["is_within_limit"])
        self.assertEqual(out["1"]["flags"], ["contract"])
        self.assertTrue(out["2"]["is_within_limit"])   # unknown years -> assume within
        self.assertNotIn("99", out)

    def test_parse_garbage(self):
        with self.assertRaises(ValueError):
            parse_results("Sorry, I can't help with that.", {"1"}, 3)


if __name__ == "__main__":
    unittest.main()
