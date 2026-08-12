#!/usr/bin/env python3
"""Safely list or update the hardened install's root-owned read allowlist."""

from __future__ import annotations

import argparse
import os
import pwd
import re
import stat
import subprocess
import tempfile
from pathlib import Path


PRODUCT_ROOT = Path("/Library/Application Support/GrokBotIMessage")
HEADER = "# Root-owned read allowlist. Managed by configure_allowlist.py.\n"
PHONE_RE = re.compile(r"^[+0-9().\- ]+$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
CHAT_RE = re.compile(r"^chat[A-Za-z0-9;_+.-]+$")


def allowlist_path() -> Path:
    return PRODUCT_ROOT / "users" / str(os.getuid()) / "config" / "allowed_chats.txt"


def validate_entry(value: str) -> str:
    entry = value.strip()
    if not entry or len(entry) > 200 or any(ord(char) < 32 for char in entry):
        raise ValueError("entry must be 1..200 characters with no control characters")
    digits = re.sub(r"[^0-9]", "", entry)
    if PHONE_RE.fullmatch(entry) and len(digits) >= 10:
        return entry
    if EMAIL_RE.fullmatch(entry) or CHAT_RE.fullmatch(entry):
        return entry
    raise ValueError("entry must be a phone number, email address, or chat identifier")


def read_entries(path: Path) -> list[str]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise RuntimeError(f"hardened allowlist missing: {path}")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise RuntimeError(f"hardened allowlist missing or invalid: {path}")
    return sorted(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def install_entries(path: Path, entries: list[str]) -> None:
    if path != allowlist_path():
        raise RuntimeError("refusing an unexpected policy destination")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(HEADER)
        for entry in sorted(set(entries), key=str.casefold):
            handle.write(f"{entry}\n")
        temporary = Path(handle.name)
    try:
        subprocess.run(
            [
                "/usr/bin/sudo",
                "/usr/bin/install",
                "-o",
                "root",
                "-g",
                "wheel",
                "-m",
                "600",
                str(temporary),
                str(path),
            ],
            check=True,
        )
        subprocess.run(["/usr/bin/sudo", "/bin/chmod", "-N", str(path)], check=True)
        subprocess.run(
            [
                "/usr/bin/sudo",
                "/bin/chmod",
                "+a",
                f"user:{pwd.getpwuid(os.getuid()).pw_name} allow read",
                str(path),
            ],
            check=True,
        )
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("add", "remove", "list"))
    parser.add_argument("entry", nargs="?")
    args = parser.parse_args()
    path = allowlist_path()
    entries = read_entries(path)

    if args.action == "list":
        if args.entry is not None:
            parser.error("list does not accept an entry")
        print("\n".join(entries))
        return 0
    if args.entry is None:
        parser.error(f"{args.action} requires an entry")

    entry = validate_entry(args.entry)
    if args.action == "add":
        entries.append(entry)
    else:
        entries = [existing for existing in entries if existing.casefold() != entry.casefold()]
    install_entries(path, entries)
    print(f"{args.action} complete: {entry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
