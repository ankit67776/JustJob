import os
import tempfile
import unittest
from pathlib import Path

from justjob.config import load_env


class EnvTests(unittest.TestCase):
    def test_load_env(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text(
                "JF_EMPTY=          # https://example.com\n"
                "JF_SET=abc123   # comment\n"
                "JF_QUOTED='a # b'\n"
                "# JF_COMMENTED=x\n",
                encoding="utf-8",
            )
            for k in ("JF_EMPTY", "JF_SET", "JF_QUOTED", "JF_COMMENTED"):
                os.environ.pop(k, None)
            load_env(p)
            self.assertNotIn("JF_EMPTY", os.environ)
            self.assertEqual(os.environ["JF_SET"], "abc123")
            self.assertEqual(os.environ["JF_QUOTED"], "a # b")
            self.assertNotIn("JF_COMMENTED", os.environ)


if __name__ == "__main__":
    unittest.main()
