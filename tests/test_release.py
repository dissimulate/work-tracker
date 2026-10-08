"""Checks for scripts/release.py and the release record it keeps: CHANGELOG.md and the manifest version."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import release  # noqa: E402

CHANGELOG = """# Changelog

Intro.

## Unreleased

### Fixed

- A fix
  on two lines.

## 0.1.0 - 2026-01-01

### Added

- First.
"""


class ReleaseRecord(unittest.TestCase):
    def test_changelog_names_the_manifest_version(self):
        """The newest released section is the version installed copies are cached by."""
        text = (ROOT / release.CHANGELOG).read_text()
        _, _, after = release.split_unreleased(text)
        newest = after.split("\n", 1)[0].split()[1]
        self.assertEqual(newest, release.manifest_version((ROOT / release.MANIFEST).read_text()))


class Release(unittest.TestCase):
    def test_next_version(self):
        self.assertEqual([release.next_version("1.2.3", b) for b in ("patch", "minor", "major", "1.3.0")],
                         ["1.2.4", "1.3.0", "2.0.0", "1.3.0"])
        for bump in ("1.2.3", "1.0.0", "next"):
            with self.subTest(bump=bump), self.assertRaises(release.Stop):
                release.next_version("1.2.3", bump)

    def test_unreleased_lines_join_and_the_section_takes_their_place(self):
        head, entries, after = release.split_unreleased(CHANGELOG)
        self.assertEqual(entries, [("Fixed", "A fix on two lines.")])
        self.assertTrue(head.endswith("## Unreleased\n") and after.startswith("## 0.1.0"))
        section = release.notes([("Added", "New."), *entries, ("Added", "Newer.")])
        self.assertEqual(section, "### Added\n\n- New.\n- Newer.\n\n### Fixed\n\n- A fix on two lines.")
        out = release.released(CHANGELOG, "0.2.0", "2026-02-02", section)
        self.assertIn("## Unreleased\n\n## 0.2.0 - 2026-02-02\n\n### Added\n\n- New.", out)
        self.assertTrue(out.endswith("- A fix on two lines.\n\n## 0.1.0 - 2026-01-01\n\n### Added\n\n- First.\n"))
        self.assertEqual(release.split_unreleased(out)[1], [])

    def test_unreleased_rejects_a_line_it_cannot_place(self):
        for body in ("### Tweaked\n\n- x\n", "- x\n", "### Added\n\nloose\n"):
            with self.subTest(body=body), self.assertRaises(release.Stop):
                release.split_unreleased(f"# C\n\n## Unreleased\n\n{body}")


if __name__ == "__main__":
    unittest.main()
