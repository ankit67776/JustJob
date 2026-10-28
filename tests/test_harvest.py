"""Company discovery from the Common Crawl index, and the companies/ registry."""
import gzip
import io
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from justjob import harvest, registry
from justjob.net import HttpError
from justjob.sources import build_tasks

from .helpers import make_cfg


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class HarvestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(registry, "DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_index_slices_include_block_before_prefix(self):
        cluster = (
            "co,example)/ 2026\tcdx-00001.gz\t0\t100\t1\n"
            "io,greenhouse,boards)/acme 2026\tcdx-00001.gz\t100\t100\t2\n"
            "io,greenhouse,boards)/zeta 2026\tcdx-00001.gz\t200\t100\t3\n"
            "io,hubspot)/ 2026\tcdx-00001.gz\t300\t100\t4\n"
        ).encode()
        with mock.patch.object(harvest.urllib.request, "urlopen", return_value=FakeResponse(cluster)):
            slices = harvest.index_slices("CC-X", {"greenhouse": ("io,greenhouse,",)})
        self.assertEqual(slices["greenhouse"], [("cdx-00001.gz", 0, 100), ("cdx-00001.gz", 100, 100),
                                                ("cdx-00001.gz", 200, 100)])

    def test_read_slice_and_slugs(self):
        lines = [
            'io,greenhouse,boards)/acme/jobs/1 2026 {"url": "https://boards.greenhouse.io/acme/jobs/1"}',
            'io,greenhouse,boards)/acme 2026 {"url": "https://boards.greenhouse.io/acme"}',
            'io,greenhouse,boards)/embed 2026 {"url": "https://boards.greenhouse.io/embed/job_board?for=beta"}',
            'io,greenhouse,boards)/v1 2026 {"url": "https://boards.greenhouse.io/v1/x"}',
        ]
        block = gzip.compress("\n".join(lines).encode())
        with mock.patch.object(harvest, "_get", return_value=block):
            urls = harvest.read_slice("CC-X", ("cdx.gz", 0, len(block)))
        self.assertEqual(harvest.slugs_in("greenhouse", urls), Counter({"acme": 2, "beta": 1}))
        wd = harvest.slugs_in("workday", ["https://acme.wd3.myworkdayjobs.com/en-US/Ext/job/x",
                                          "https://www.wd3.myworkdayjobs.com/x"])
        self.assertEqual(wd, Counter({"acme/wd3/ext": 1}))

    def test_candidates_skip_known_and_recently_dead(self):
        registry.save("greenhouse", {"known": ("Known", 3)})
        cfg = make_cfg()
        cfg["sources"] = {"greenhouse": {"boards": {"configured": "C"}}}
        found = {"greenhouse": Counter({"new": 5, "known": 9, "configured": 2, "dead": 1, "old-dead": 1})}
        checked = {("greenhouse", "dead"): "2999-01-01", ("greenhouse", "old-dead"): "2000-01-01"}
        todo = harvest.candidates(cfg, found, 30, checked)
        self.assertEqual(todo, {"greenhouse": ["new", "old-dead"]})

    def test_verify_saves_live_and_remembers_dead(self):
        def probe(slug):
            if slug == "broken":
                raise HttpError(404, "https://x", "")
            if slug == "busy":
                raise HttpError(429, "https://x", "")
            return {"live": (4, "Live Inc"), "empty": (0, "")}[slug]
        checked = {}
        with mock.patch.object(harvest.ATS["greenhouse"], "probe", side_effect=probe), \
                mock.patch.object(harvest, "CHECKED", Path(self.tmp.name) / "checked.tsv"), \
                mock.patch.object(harvest, "save_checked"):
            live = harvest.verify(make_cfg(), {"greenhouse": ["live", "empty", "broken", "busy"]}, checked=checked)
        self.assertEqual(live, {"greenhouse": {"live": ("Live Inc", 4)}})
        self.assertEqual(registry.load("greenhouse"), {"live": ("Live Inc", 4)})
        self.assertEqual(set(checked), {("greenhouse", "empty"), ("greenhouse", "broken")})   # not "busy"

    def test_build_tasks_merges_registry_with_config(self):
        registry.save("greenhouse", {"acme": ("acme from harvest", 3), "beta": ("Beta", 1)})
        cfg = make_cfg()
        cfg["fetch"]["registry"] = True
        cfg["sources"] = {"greenhouse": {"enabled": True, "boards": {"acme": "Acme"}}}
        labels = sorted(label for label, _ in build_tasks(cfg))
        self.assertEqual(labels, ["greenhouse:acme", "greenhouse:beta"])
        cfg["fetch"]["registry"] = False
        self.assertEqual([label for label, _ in build_tasks(cfg)], ["greenhouse:acme"])

    def test_registry_roundtrip(self):
        self.assertEqual(registry.add("gem", {"a": ("A\tCo", 2)}), 1)
        self.assertEqual(registry.add("gem", {"a": ("A Co", 5), "b": ("B", 1)}), 1)
        self.assertEqual(registry.load("gem"), {"a": ("A Co", 5), "b": ("B", 1)})
        self.assertEqual(registry.boards("gem"), {"a": "A Co", "b": "B"})


if __name__ == "__main__":
    unittest.main()
