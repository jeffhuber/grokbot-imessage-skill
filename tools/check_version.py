#!/usr/bin/env python3
"""Verify that a release tag matches all shipped component versions."""

from __future__ import annotations

import argparse
import ast
import re
from datetime import date
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def helper_version() -> str:
    module = ast.parse((REPO_ROOT / "bin" / "helper.py").read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "HELPER_VERSION":
                    value = ast.literal_eval(node.value)
                    if isinstance(value, str):
                        return value
    raise RuntimeError("HELPER_VERSION not found in bin/helper.py")


def frontmatter_version(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise RuntimeError(f"YAML frontmatter not found in {path}")
    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], 1)
            if line == "---"
        )
    except StopIteration as error:
        raise RuntimeError(f"YAML frontmatter is not closed in {path}") from error
    matches = re.findall(
        r"^version:[ \t]*([^ \t\r\n]+)[ \t]*$",
        "\n".join(lines[1:end]),
        re.MULTILINE,
    )
    if len(matches) != 1:
        raise RuntimeError(f"expected one frontmatter version in {path}")
    return matches[0]


def skill_version() -> str:
    return frontmatter_version(REPO_ROOT / "SKILL.md")


def check_changelog(expected_version: str) -> tuple[bool, str]:
    content = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    pattern = (
        rf"^##[ \t]+{re.escape(expected_version)}[ \t]+-[ \t]+"
        r"([^\r\n]*?)[ \t]*\r?$"
    )
    matches = re.findall(pattern, content, re.MULTILINE)
    if len(matches) != 1:
        return (
            False,
            f"CHANGELOG.md must contain exactly one dated heading for "
            f"version {expected_version}; found {len(matches)}",
        )
    changelog_date = matches[0]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", changelog_date) is None:
        return False, f"CHANGELOG.md has invalid date {changelog_date!r}"
    try:
        date.fromisoformat(changelog_date)
    except ValueError:
        return False, f"CHANGELOG.md has invalid date {changelog_date!r}"
    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="release tag, for example v1.1.0")
    args = parser.parse_args()

    expected = args.tag[1:] if args.tag.startswith("v") else args.tag
    versions = {"helper": helper_version(), "skill": skill_version()}
    mismatches = {name: version for name, version in versions.items() if version != expected}
    if mismatches:
        for name, version in mismatches.items():
            print(f"{name} version {version!r} does not match tag {args.tag!r}")
        return 1
    changelog_ok, changelog_error = check_changelog(expected)
    if not changelog_ok:
        print(changelog_error)
        return 1
    print(f"release versions match {args.tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
