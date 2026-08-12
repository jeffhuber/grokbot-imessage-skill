#!/usr/bin/env python3
"""Verify that a legacy LaunchAgent plist belongs to this installation."""

from __future__ import annotations

import argparse
import os
import pathlib
import plistlib
import stat


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plist", required=True, type=pathlib.Path)
    parser.add_argument("--program", required=True)
    parser.add_argument("--watch", required=True)
    args = parser.parse_args()

    try:
        metadata = os.lstat(args.plist)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            return 1
        with args.plist.open("rb") as stream:
            payload = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException):
        return 1

    arguments = payload.get("ProgramArguments")
    watch_paths = payload.get("WatchPaths")
    if not isinstance(arguments, list) or not arguments:
        return 1
    if not isinstance(watch_paths, list):
        return 1

    expected_program = os.path.abspath(os.path.expanduser(args.program))
    expected_watch = os.path.abspath(os.path.expanduser(args.watch))
    actual_program = os.path.abspath(os.path.expanduser(str(arguments[0])))
    actual_watches = {
        os.path.abspath(os.path.expanduser(str(path))) for path in watch_paths
    }
    return 0 if actual_program == expected_program and expected_watch in actual_watches else 1


if __name__ == "__main__":
    raise SystemExit(main())
