"""
Tests for the master (fan-in) config: several airgapped subordinates
pushing up into one read-only master.

The validation under test here is the most consequential in the whole
tool. Every import treats anything in its target section that is absent
from its snapshot as an orphan -- reported as drift, and DELETED once
`delete_drift` is on. So if two subordinates ever share a target section,
site A's sync sees site B's subnets as orphans and removes them. There is
no partially-correct way to run that, which is why the loader refuses the
config outright rather than warning.
"""

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ipamsync.config import ConfigError, load_config, resolve_sources  # noqa: E402


def write_config(text):
    handle = tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False)
    handle.write(textwrap.dedent(text))
    handle.close()
    return handle.name


MASTER = """
    target:
      base_url: https://master.example.com
      app_id: sync
      token_env: TOK
    sources:
      - name: site-a
        snapshot_dir: /srv/ipam/site-a
        sections: [Networks]
        section_map: {Networks: Site-A}
      - name: site-b
        snapshot_dir: /srv/ipam/site-b
        sections: [Networks]
        section_map: {Networks: Site-B}
"""


class TestMasterConfig(unittest.TestCase):
    def test_loads_and_resolves_each_source(self):
        config = load_config(write_config(MASTER))
        sources = resolve_sources(config)
        self.assertEqual([name for name, _ in sources], ["site-a", "site-b"])

    def test_each_source_gets_its_own_section_map_and_path(self):
        config = load_config(write_config(MASTER))
        by_name = dict(resolve_sources(config))
        self.assertEqual(by_name["site-a"]["section_map"], {"Networks": "Site-A"})
        self.assertEqual(by_name["site-b"]["snapshot_dir"], "/srv/ipam/site-b")

    def test_every_source_shares_the_one_target_and_options(self):
        config = load_config(write_config(MASTER))
        for _, source in resolve_sources(config):
            self.assertEqual(source["target"]["base_url"],
                             "https://master.example.com")
            self.assertIs(source["options"], config["options"])

    def test_a_single_source_can_be_selected(self):
        config = load_config(write_config(MASTER))
        self.assertEqual([n for n, _ in resolve_sources(config, only="site-b")],
                         ["site-b"])

    def test_unknown_source_name_is_rejected_and_lists_the_real_ones(self):
        config = load_config(write_config(MASTER))
        with self.assertRaises(ConfigError) as ctx:
            resolve_sources(config, only="site-z")
        self.assertIn("site-a", str(ctx.exception))


class TestFanInSafetyValidation(unittest.TestCase):
    """The guards that stop one subordinate destroying another's records."""

    def test_two_sources_sharing_a_target_section_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_config("""
                target: {base_url: https://m, app_id: sync, token_env: T}
                sources:
                  - name: site-a
                    snapshot_dir: /srv/a
                    sections: [Networks]
                    section_map: {Networks: Shared}
                  - name: site-b
                    snapshot_dir: /srv/b
                    sections: [Networks]
                    section_map: {Networks: Shared}
            """))
        message = str(ctx.exception)
        self.assertIn("both target the section", message)
        # The message must explain the consequence, not just the rule --
        # someone hitting this needs to understand why it is fatal.
        self.assertIn("DELETED", message)

    def test_sharing_is_detected_case_insensitively(self):
        # phpIPAM section names are free text; 'Shared' and 'shared' are
        # the same section as far as the importer's lookup is concerned.
        with self.assertRaises(ConfigError):
            load_config(write_config("""
                target: {base_url: https://m, app_id: sync, token_env: T}
                sources:
                  - name: site-a
                    snapshot_dir: /srv/a
                    sections: [Networks]
                    section_map: {Networks: Shared}
                  - name: site-b
                    snapshot_dir: /srv/b
                    sections: [Networks]
                    section_map: {Networks: shared}
            """))

    def test_unmapped_sections_collide_too(self):
        # With no section_map the source's own section name is used, so
        # two subordinates that both call their section "Networks" would
        # land in the same place on the master.
        with self.assertRaises(ConfigError):
            load_config(write_config("""
                target: {base_url: https://m, app_id: sync, token_env: T}
                sources:
                  - name: site-a
                    snapshot_dir: /srv/a
                    sections: [Networks]
                  - name: site-b
                    snapshot_dir: /srv/b
                    sections: [Networks]
            """))

    def test_negative_control_distinct_sections_are_accepted(self):
        # Proves the collision checks are triggered by the collision, not
        # by the loader rejecting every multi-source config.
        config = load_config(write_config(MASTER))
        self.assertEqual(len(resolve_sources(config)), 2)

    def test_two_sources_reading_the_same_directory_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_config("""
                target: {base_url: https://m, app_id: sync, token_env: T}
                sources:
                  - name: site-a
                    snapshot_dir: /srv/shared
                    sections: [Networks]
                    section_map: {Networks: Site-A}
                  - name: site-b
                    snapshot_dir: /srv/shared
                    sections: [Networks]
                    section_map: {Networks: Site-B}
            """))
        self.assertIn("both read", str(ctx.exception))

    def test_duplicate_source_names_are_refused(self):
        with self.assertRaises(ConfigError):
            load_config(write_config("""
                target: {base_url: https://m, app_id: sync, token_env: T}
                sources:
                  - name: site-a
                    snapshot_dir: /srv/a
                    sections: [Networks]
                    section_map: {Networks: Site-A}
                  - name: site-a
                    snapshot_dir: /srv/b
                    sections: [Networks]
                    section_map: {Networks: Site-B}
            """))

    def test_a_source_with_no_sections_is_refused(self):
        # Otherwise it would silently own nothing and sync nothing.
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_config("""
                target: {base_url: https://m, app_id: sync, token_env: T}
                sources:
                  - name: site-a
                    snapshot_dir: /srv/a
            """))
        self.assertIn("declares no sections", str(ctx.exception))

    def test_missing_snapshot_dir_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_config("""
                target: {base_url: https://m, app_id: sync, token_env: T}
                sources:
                  - name: site-a
                    sections: [Networks]
            """))
        self.assertIn("snapshot_dir", str(ctx.exception))


class TestSingleSourceStillWorks(unittest.TestCase):
    """The subordinate-side config is unchanged by any of this."""

    def test_single_source_config_loads(self):
        config = load_config(write_config("""
            source: {base_url: https://a, app_id: sync, token_env: T}
            target: {base_url: https://b, app_id: sync, token_env: T}
            sections: [Shared]
        """))
        self.assertEqual(config["sections"], ["Shared"])
        self.assertEqual(resolve_sources(config), [])

    def test_config_with_neither_sections_nor_sources_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_config("""
                target: {base_url: https://b, app_id: sync, token_env: T}
            """))
        self.assertIn("sources", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
