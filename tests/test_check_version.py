from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
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

    def test_shared_core_version_requires_a_string(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grok-version-check-") as td:
            root = Path(td)
            (root / "shared-core.json").write_text(
                '{"identity":{"helper_version":120}}', encoding="utf-8"
            )
            with mock.patch.object(check_version, "REPO_ROOT", root):
                with self.assertRaisesRegex(RuntimeError, "identity.helper_version"):
                    check_version.shared_core_version()

    def test_shared_core_mismatch_fails_release_check(self) -> None:
        versions = {
            "helper_version": "1.2.0",
            "skill_version": "1.2.0",
            "shared_core_version": "1.1.0",
            "check_changelog": (True, ""),
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("sys.argv", ["check_version.py", "v1.2.0"]))
            stack.enter_context(mock.patch("builtins.print"))
            for name, value in versions.items():
                stack.enter_context(
                    mock.patch.object(check_version, name, return_value=value)
                )
            self.assertEqual(check_version.main(), 1)


if __name__ == "__main__":
    unittest.main()
