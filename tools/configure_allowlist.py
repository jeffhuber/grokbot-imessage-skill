#!/usr/bin/env python3
"""Safely list or update the hardened install's root-owned read allowlist."""

from __future__ import annotations

import argparse
import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


PRODUCT_ROOT = Path("/Library/Application Support/GrokBotIMessage")
HEADER = "# Root-owned read allowlist. Managed by configure_allowlist.py.\n"
PHONE_RE = re.compile(r"^[+0-9().\- ]+$")
EMAIL_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
EMAIL_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
EMAIL_RE = re.compile(
    rf"^{EMAIL_ATOM}(?:\.{EMAIL_ATOM})*@{EMAIL_LABEL}(?:\.{EMAIL_LABEL})+$"
)
CHAT_RE = re.compile(r"^chat[A-Za-z0-9;_+.-]+$")


def _normalize_macos_firmlinks(path: str) -> str:
    """Normalize macOS firmlinks without following user symlinks.
    
    On macOS, /var and /tmp are firmlinks to /private/var and /private/tmp.
    This function normalizes these OS-managed paths without resolving user symlinks.
    Only applies normalization if the firmlink actually exists on the system.
    """
    # Check /var firmlink
    if (path.startswith("/var/") or path == "/var") and os.path.exists("/private/var"):
        # Only normalize if /var actually resolves to /private/var on this system
        if os.path.realpath("/var") == "/private/var":
            return ("/private" + path) if path.startswith("/var/") else "/private/var"
    
    # Check /tmp firmlink  
    if (path.startswith("/tmp/") or path == "/tmp") and os.path.exists("/private/tmp"):
        # Only normalize if /tmp actually resolves to /private/tmp on this system
        if os.path.realpath("/tmp") == "/private/tmp":
            return ("/private" + path) if path.startswith("/tmp/") else "/private/tmp"
    
    return path


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
    expected_path = allowlist_path()
    if path != expected_path:
        raise RuntimeError("refusing an unexpected policy destination")
    
    # Reject if PRODUCT_ROOT itself is a symlink
    if PRODUCT_ROOT.exists() and PRODUCT_ROOT.is_symlink():
        raise RuntimeError("PRODUCT_ROOT must not be a symlink")
    
    # Compute unresolved absolute expected paths with firmlink-only normalization.
    # Uses abspath (not realpath) to avoid following user symlinks in PRODUCT_ROOT,
    # then normalizes only OS-managed firmlinks (/var ↔ /private/var, /tmp ↔ /private/tmp).
    # This catches attacks where PRODUCT_ROOT or subdirs are symlinked.
    expected_abs = _normalize_macos_firmlinks(os.path.abspath(str(expected_path)))
    expected_parent_abs = _normalize_macos_firmlinks(os.path.abspath(str(expected_path.parent)))
    
    # Pre-install symlink check: reject if path exists and is a symlink
    if path.exists():
        if path.is_symlink():
            raise RuntimeError("allowlist path must not be a symlink")
        # Compare abspath vs realpath on the pre-resolve path
        if os.path.abspath(str(path)) != os.path.realpath(str(path)):
            raise RuntimeError("allowlist path must not be a symlink")
    else:
        # Path doesn't exist yet - verify parent is what we expect.
        # Use O_NOFOLLOW + O_CLOEXEC to validate parent components securely.
        try:
            parent_fd = os.open(
                str(path.parent),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            try:
                parent_stat = os.fstat(parent_fd)
                if not stat.S_ISDIR(parent_stat.st_mode):
                    raise RuntimeError("allowlist parent is not a directory")
            finally:
                os.close(parent_fd)
        except OSError as exc:
            raise RuntimeError(f"allowlist parent validation failed: {exc}")
        
        # Compare resolved actual parent against firmlink-normalized expected parent.
        # Security: expected_parent_abs is computed from the PRODUCT_ROOT constant with
        # only firmlink normalization (realpath on the unresolved abspath). If PRODUCT_ROOT
        # or its subdirs are symlinked, the resolved actual won't match the expected structure.
        parent_canonical = os.path.realpath(str(path.parent))
        if parent_canonical != expected_parent_abs:
            raise RuntimeError("allowlist parent directory mismatch")
    
    # Create temp file in a user-owned private directory to avoid replacement race.
    # In hardened installs, the allowlist parent is root-owned, so we create a
    # secure staging directory under /tmp with mode 0o700.
    temp_dir_path = tempfile.mkdtemp(prefix="grokbot-allowlist-", suffix=".tmp")
    temp_dir = Path(temp_dir_path)
    
    try:
        fd = os.open(temp_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            metadata = os.fstat(fd)
            if metadata.st_uid != os.getuid():
                raise RuntimeError("staging directory must be owned by current user")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise RuntimeError("staging directory must not have group/world permissions")
        finally:
            os.close(fd)
        
        # Write to a tempfile in the private staging directory
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=temp_dir) as handle:
            handle.write(HEADER)
            for entry in sorted(set(entries), key=str.casefold):
                handle.write(f"{entry}\n")
            temporary = Path(handle.name)
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
        
        # Post-install verification with lstat: must not be a symlink
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError("installed allowlist is not a regular file")
            if os.path.abspath(str(path)) != os.path.realpath(str(path)):
                raise RuntimeError("installed allowlist is a symlink")
            # Compare resolved actual path against firmlink-normalized expected path.
            # Security maintained: expected_abs is from PRODUCT_ROOT constant + firmlink normalize.
            post_install_canonical = os.path.realpath(str(path.resolve(strict=True)))
            if post_install_canonical != expected_abs:
                raise RuntimeError("allowlist was not created at expected location")
        except OSError as e:
            raise RuntimeError(f"allowlist verification failed: {e}")
        
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
        try:
            shutil.rmtree(temp_dir)
        except OSError:
            pass


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
