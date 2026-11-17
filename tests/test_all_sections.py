"""`sections: all` exports every section; a list still scopes the export."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ipamsync.config import load_config  # noqa: E402
from test_end_to_end import make_config, populated_source, run_export  # noqa: E402


class TestLoadConfig(unittest.TestCase):
    def _load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            f.write(text)
        self.addCleanup(os.unlink, f.name)
        return load_config(f.name)

    def test_all_sets_flag(self):
        cfg = self._load("source: {}\nsections: all\n")
        self.assertTrue(cfg["all_sections"])
        self.assertEqual(cfg["sections"], [])

    def test_list_does_not_set_flag(self):
        cfg = self._load("source: {}\nsections: [Shared]\n")
        self.assertFalse(cfg["all_sections"])
        self.assertEqual(cfg["sections"], ["Shared"])


class TestExportAllSections(unittest.TestCase):
    def setUp(self):
        self.source, _ = populated_source()
        lab = self.source.add_section("Lab")
        self.source.add_subnet(lab, "192.168.50.0", 24, description="lab")
        self.source.add_section("Empty")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_all_exports_every_section(self):
        config = {**make_config(sections=()), "all_sections": True}
        manifest = run_export(self.source, self.tmp.name, config)
        self.assertEqual(manifest["sections"], ["Empty", "Lab", "Shared"])
        self.assertEqual(manifest["subnet_count"], 3)

    def test_list_still_scopes_to_one_section(self):
        manifest = run_export(self.source, self.tmp.name, make_config(sections=("Lab",)))
        self.assertEqual(manifest["sections"], ["Lab"])
        self.assertEqual(manifest["subnet_count"], 1)
        self.assertFalse(list(Path(self.tmp.name).rglob("10.20.*")))


if __name__ == "__main__":
    unittest.main()
