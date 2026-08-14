from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import check_version


class ReleaseVersionTests(unittest.TestCase):
    def check(self, content: str, version: str = "1.2.0") -> tuple[bool, str]:
        with tempfile.TemporaryDirectory(prefix="grok-version-check-") as td:
            root = Path(td)
            (root / "CHANGELOG.md").write_text(content, encoding="utf-8")
            with mock.patch.object(check_version, "REPO_ROOT", root):
                return check_version.check_changelog(version)

    def test_historical_release_date_remains_valid(self) -> None:
        self.assertEqual(
            self.check("# Changelog\n\n## 1.2.0 - 2020-01-02\n"),
            (True, ""),
        )

    def test_crlf_release_heading_is_valid(self) -> None:
        self.assertEqual(
            self.check("# Changelog\r\n\r\n## 1.2.0 - 2020-01-02\r\n"),
            (True, ""),
        )

    def test_duplicate_version_headings_are_rejected(self) -> None:
        valid, error = self.check(
            "## 1.2.0 - 2020-01-02\n\n## 1.2.0 - 2020-01-03\n"
        )
        self.assertFalse(valid)
        self.assertIn("found 2", error)

    def test_invalid_calendar_date_is_rejected(self) -> None:
        valid, error = self.check("## 1.2.0 - 2020-02-31\n")
        self.assertFalse(valid)
        self.assertIn("invalid date", error)

    def test_trailing_date_text_is_rejected(self) -> None:
        valid, error = self.check("## 1.2.0 - 2020-01-02 released\n")
        self.assertFalse(valid)
        self.assertIn("invalid date", error)

    def test_heading_cannot_span_lines(self) -> None:
        valid, error = self.check("## 1.2.0\n  - 2020-01-02\n")
        self.assertFalse(valid)
        self.assertIn("found 0", error)

    def test_missing_version_heading_is_rejected(self) -> None:
        valid, error = self.check("## 1.1.0 - 2020-01-02\n")
        self.assertFalse(valid)
        self.assertIn("found 0", error)

    def test_skill_version_is_read_only_from_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grok-skill-version-") as td:
            skill = Path(td) / "SKILL.md"
            skill.write_text(
                "---\nname: example\nversion: 1.2.0\n---\n\nversion: 9.9.9\n",
                encoding="utf-8",
            )
            self.assertEqual(check_version.frontmatter_version(skill), "1.2.0")

    def test_indented_separator_does_not_close_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grok-skill-version-") as td:
            skill = Path(td) / "SKILL.md"
            skill.write_text(
                "---\nname: example\n  ---\nversion: 1.2.0\n---\n",
                encoding="utf-8",
            )
            self.assertEqual(check_version.frontmatter_version(skill), "1.2.0")


if __name__ == "__main__":
    unittest.main()
