#!/usr/bin/env python3
"""Grok Bot iMessage helper

Runs on macOS, triggered by launchd when a new file lands in control/requests/.
Scans the request queue, dispatches each whitelisted action against a snapshot
of ~/Library/Messages/chat.db, writes a response JSON into control/responses/,
and deletes the request.

Security posture:
  - Actions are strictly whitelisted (no eval/exec/shell-out).
  - All SQL uses parameterized queries.
  - chat.db is copied to a per-run tempfile (cleaned up on exit).
  - Read policy is applied before any message text is returned.
  - 2FA codes, card numbers, and SSN patterns are redacted in responses.
  - Response writes are atomic (tmp + rename) so the agent never reads a
    half-written file.

This script should be invoked only by the signed `grokbot-imessage-helper`
wrapper. Running it directly still works but without the environment
hardening the wrapper provides.
"""

from __future__ import annotations

import fcntl
import glob
import json
import os
import re
import sqlite3
import stat
import struct
import sys
import tempfile
import time
import traceback
import uuid
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CODE_ROOT = Path(__file__).resolve().parent.parent
if "IMESSAGE_BRIDGE_DIR" in os.environ:
    _bridge_root_value = os.environ["IMESSAGE_BRIDGE_DIR"]
elif "COWORK_IMESSAGE_BRIDGE_DIR" in os.environ:
    _bridge_root_value = os.environ["COWORK_IMESSAGE_BRIDGE_DIR"]
else:
    _bridge_root_value = str(CODE_ROOT)
if not _bridge_root_value:
    raise RuntimeError(
        "IMESSAGE_BRIDGE_DIR is required "
        "(COWORK_IMESSAGE_BRIDGE_DIR remains a one-release compatibility alias)"
    )
BRIDGE_ROOT = Path(
    os.path.abspath(os.path.expanduser(_bridge_root_value))
)
POLICY_ROOT = Path(
    os.path.abspath(
        os.path.expanduser(
            os.environ.get("IMESSAGE_POLICY_DIR")
            or str(BRIDGE_ROOT / "contacts")
        )
    )
)
REQUESTS_DIR = BRIDGE_ROOT / "control" / "requests"
RESPONSES_DIR = BRIDGE_ROOT / "control" / "responses"
LOG_PATH = BRIDGE_ROOT / "control" / "log.txt"
BLOCKLIST_PATH = POLICY_ROOT / "blocked_chats.txt"
ALLOWLIST_PATH = Path(
    os.path.abspath(
        os.path.expanduser(
            os.environ.get("COWORK_IMESSAGE_READ_ALLOWLIST")
            or str(
                POLICY_ROOT / "allowed_chats.txt"
            )
        )
    )
)
READ_POLICY_PATH = POLICY_ROOT / "read_policy.txt"
SEND_POLICY_PATH = POLICY_ROOT / "send_policy.json"
SEND_GATE_PATH = Path(
    os.path.abspath(
        os.path.expanduser(
            os.environ.get("IMESSAGE_SEND_GATE_PATH")
            or str(CODE_ROOT / "bin" / "send_gate.py")
        )
    )
)
CONFIRM_HELPER_PATH = Path(
    os.path.abspath(
        os.path.expanduser(
            os.environ.get("IMESSAGE_CONFIRM_HELPER_PATH")
            or str(CODE_ROOT / "bin" / "grokbot-imessage-confirm")
        )
    )
)
CHAT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"
HOST_DISPLAY_NAME = os.environ.get("IMESSAGE_HOST_DISPLAY_NAME", "Grok Bot")
PRODUCT_ID = os.environ.get("IMESSAGE_PRODUCT_ID", "grokbot-imessage")

# Detect wrapper mode: product if any IMESSAGE_* product vars set, else baked
_PRODUCT_ENV_VARS = (
    "IMESSAGE_PRODUCT_ID",
    "IMESSAGE_POLICY_DIR",
    "IMESSAGE_SEND_GATE_PATH",
    "IMESSAGE_CONFIRM_HELPER_PATH",
)
WRAPPER_MODE = "product" if any(v in os.environ for v in _PRODUCT_ENV_VARS) else "baked"

HELPER_VERSION = "1.2.2"
PROTOCOL_VERSION = "1.2"

# Bridge role. The DIY install and every host bridge run as "host". A
# management bridge (product mode, IMESSAGE_BRIDGE_ROLE=manager) is the only
# place `list_chats` is served, and it never serves body-returning actions.
# The table below is enforced in the worker; hiding an action in a host or
# app layer is not sufficient. Unknown role values fail closed.
_BRIDGE_ROLE_ENV = "IMESSAGE_BRIDGE_ROLE"
DEFAULT_BRIDGE_ROLE = "host"
_HOST_ACTIONS = (
    "status",
    "review",
    "search",
    "chat_history",
    "response_stats",
    "contacts_lookup",
    "send_preview",
    "send",
)
_MANAGER_ACTIONS = ("status", "contacts_lookup", "list_chats")
ROLE_ACTIONS: dict[str, tuple[str, ...]] = {
    "host": _HOST_ACTIONS,
    "manager": _MANAGER_ACTIONS,
}


def bridge_role() -> str:
    """Return the configured bridge role (unvalidated; see allowed_actions)."""
    value = os.environ.get(_BRIDGE_ROLE_ENV, "")
    return value.strip().lower() or DEFAULT_BRIDGE_ROLE


def allowed_actions(role: str | None = None) -> tuple[str, ...]:
    """Actions the worker will serve for `role`. Unknown roles get none."""
    return ROLE_ACTIONS.get(role if role is not None else bridge_role(), ())

# ---------------------------------------------------------------------------
# Sibling module loading
# ---------------------------------------------------------------------------
# The C wrapper runs python3 with `-I` (isolated mode), which deliberately
# prevents sys.path[0] from being set to this file's directory — it blocks
# the "drop a malicious foo.py into bin/ and watch helper.py import it"
# attack. We honor that hardening by loading our known-good sibling module
# by its absolute baked-in path rather than by ordinary import.
import importlib.util as _importlib_util  # noqa: E402


def _load_sibling(name: str):
    path = CODE_ROOT / "bin" / f"{name}.py"
    spec = _importlib_util.spec_from_file_location(name, path)
    mod = _importlib_util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_send_gate():
    # Wrapper validate_file covers SEND_GATE_PATH ownership and permissions;
    # we load by absolute path to honor IMESSAGE_SEND_GATE_PATH override.
    spec = _importlib_util.spec_from_file_location("send_gate", SEND_GATE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load send_gate from {SEND_GATE_PATH}")
    mod = _importlib_util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Route send_gate's state to the bridge so the send gate knows where to write
# nonces. Preserve explicit values and retain the legacy name as a
# compatibility input. Empty explicit values have already failed closed above.
if "IMESSAGE_BRIDGE_DIR" not in os.environ and "COWORK_IMESSAGE_BRIDGE_DIR" not in os.environ:
    os.environ["IMESSAGE_BRIDGE_DIR"] = str(BRIDGE_ROOT)
_send_gate = _load_send_gate()
SEND_NONCE_TTL = _send_gate.SEND_NONCE_TTL
SendGateError = _send_gate.SendGateError
mint_send_nonce = _send_gate.mint_send_nonce
consume_send_nonce = _send_gate.consume_send_nonce
reap_expired_nonces = _send_gate.reap_expired_nonces

APPLE_EPOCH = 978_307_200  # seconds between 1970-01-01 and 2001-01-01

# Parameter bounds. Over these limits we reject rather than return partial data.
MAX_DAYS = 90
MAX_HOURS = 24 * 30
MAX_LIMIT = 500
MAX_SEARCH_LEN = 200
# list_chats has its own window: it returns no bodies, only which threads
# exist, so a multi-year window is safe and useful for policy discovery.
MAX_LIST_CHATS_DAYS = 3650
LIST_CHATS_DEFAULT_DAYS = 365
LIST_CHATS_DEFAULT_LIMIT = 200
LIST_CHATS_FILTER_SCAN_LIMIT = 500
MAX_LIST_CHATS_QUERY_LEN = 100
MAX_LIST_CHATS_PARTICIPANTS = 10
MAX_TEXT_SNIPPET = 600
MAX_CONTEXT_MESSAGES = 8
MAX_REQUEST_BYTES = 64 * 1024
RESPONSE_TTL_S = 60 * 60
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUP_COUNT = 3

# Send-side bounds. iMessage will accept much longer bodies, but capping here
# limits blast radius if a request is malformed or adversarial. 4000 chars is
# well above any plausible conversational message.
MAX_SEND_LEN = 4000
_SERVICE_ENUM = ("iMessage", "SMS")

# osascript timeout — the send itself is sub-second; anything much longer
# means Messages.app is hung or prompting for Automation permission.
OSASCRIPT_TIMEOUT_S = 15

import subprocess  # noqa: E402  — used only by send actions, keep the import local-ish


# ---------------------------------------------------------------------------
# Secure runtime filesystem access
# ---------------------------------------------------------------------------
_DIR_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_NOFOLLOW_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class UnsafeRuntimePath(RuntimeError):
    """Raised when user-owned bridge state is not a safe local file or directory."""


def _validate_private_directory(fd: int, label: str) -> None:
    metadata = os.fstat(fd)
    if not stat.S_ISDIR(metadata.st_mode):
        raise UnsafeRuntimePath(f"{label} is not a directory")
    if metadata.st_uid != os.getuid():
        raise UnsafeRuntimePath(f"{label} is not owned by the current user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise UnsafeRuntimePath(f"{label} must not have group/world permissions")


def _open_bridge_root() -> int:
    """Open the absolute bridge path one component at a time without symlinks."""
    root = Path(os.path.abspath(str(BRIDGE_ROOT)))
    if not root.is_absolute() or root == Path("/"):
        raise UnsafeRuntimePath("bridge root must be a non-root absolute path")

    fd = os.open("/", _DIR_OPEN_FLAGS)
    try:
        for component in root.parts[1:]:
            next_fd = os.open(component, _DIR_OPEN_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        _validate_private_directory(fd, f"bridge root {root}")
        return fd
    except UnsafeRuntimePath:
        os.close(fd)
        raise
    except OSError as exc:
        os.close(fd)
        raise UnsafeRuntimePath(f"unsafe bridge root {root}: {exc}") from exc


def _runtime_relative_parts(path: Path) -> tuple[str, ...]:
    root = Path(os.path.abspath(str(BRIDGE_ROOT)))
    candidate = Path(os.path.abspath(str(path)))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise UnsafeRuntimePath(f"runtime path escapes bridge root: {candidate}") from exc
    if any(part in ("", ".", "..") or "/" in part for part in relative.parts):
        raise UnsafeRuntimePath(f"invalid runtime path: {candidate}")
    return relative.parts


@contextmanager
def _private_directory_fd(path: Path, *, create: bool = False):
    """Yield an anchored descriptor for a private directory below the bridge."""
    parts = _runtime_relative_parts(path)
    fd = _open_bridge_root()
    try:
        for component in parts:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                next_fd = os.open(component, _DIR_OPEN_FLAGS, dir_fd=fd)
            except OSError as exc:
                raise UnsafeRuntimePath(f"unsafe runtime directory {path}: {exc}") from exc
            os.close(fd)
            fd = next_fd
            _validate_private_directory(fd, str(path))
        yield fd
    finally:
        os.close(fd)


def _validate_regular_file(fd: int, label: str, *, private: bool = False) -> os.stat_result:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafeRuntimePath(f"{label} is not a regular file")
    if metadata.st_uid != os.getuid():
        raise UnsafeRuntimePath(f"{label} is not owned by the current user")
    if private and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise UnsafeRuntimePath(f"{label} must not have group/world permissions")
    return metadata


def _stat_regular_at(directory_fd: int, name: str, *, private: bool = False) -> os.stat_result:
    metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafeRuntimePath(f"{name} is not a regular file")
    if metadata.st_uid != os.getuid():
        raise UnsafeRuntimePath(f"{name} is not owned by the current user")
    if private and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise UnsafeRuntimePath(f"{name} must not have group/world permissions")
    return metadata


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _rotate_log(control_fd: int) -> None:
    try:
        current = _stat_regular_at(control_fd, LOG_PATH.name, private=True)
    except FileNotFoundError:
        return
    if current.st_size < LOG_MAX_BYTES:
        return

    oldest = f"{LOG_PATH.name}.{LOG_BACKUP_COUNT}"
    try:
        _stat_regular_at(control_fd, oldest, private=True)
    except FileNotFoundError:
        pass
    else:
        os.unlink(oldest, dir_fd=control_fd)

    for index in range(LOG_BACKUP_COUNT - 1, 0, -1):
        source = f"{LOG_PATH.name}.{index}"
        destination = f"{LOG_PATH.name}.{index + 1}"
        try:
            _stat_regular_at(control_fd, source, private=True)
        except FileNotFoundError:
            continue
        try:
            _stat_regular_at(control_fd, destination, private=True)
        except FileNotFoundError:
            pass
        os.replace(
            source,
            destination,
            src_dir_fd=control_fd,
            dst_dir_fd=control_fd,
        )

    os.replace(
        LOG_PATH.name,
        f"{LOG_PATH.name}.1",
        src_dir_fd=control_fd,
        dst_dir_fd=control_fd,
    )


def log(msg: str) -> None:
    try:
        with _private_directory_fd(LOG_PATH.parent, create=True) as control_fd:
            _rotate_log(control_fd)
            fd = os.open(
                LOG_PATH.name,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | _FILE_NOFOLLOW_FLAGS,
                0o600,
                dir_fd=control_fd,
            )
            try:
                _validate_regular_file(fd, str(LOG_PATH), private=True)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    fd = -1
                    f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
            finally:
                if fd >= 0:
                    os.close(fd)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# attributedBody typedstream decoder (pure Python, no PyObjC)
# Ported from the original Perplexity skill and kept byte-compatible.
# ---------------------------------------------------------------------------
def _attributed_fail(data: bytes, reason: str) -> str:
    log(f"attributedBody parse failed: {reason}; bytes={len(data)}")
    return ""


def _attributed_string_at(data: bytes, idx: int) -> tuple[str, int] | None:
    p = idx + len(b"NSString") + 1

    while p < len(data) and data[p] in (0x84, 0x94, 0x85, 0x95, 0x01, 0x86):
        p += 1

    if p + 8 <= len(data) and data[p : p + 8] == b"NSObject":
        p += 8
        while p < len(data) and data[p] in (0x84, 0x94, 0x85, 0x95, 0x01, 0x86):
            p += 1

    if p < len(data) and data[p] == 0x2B:
        p += 1

    if p >= len(data):
        return None

    b0 = data[p]
    if b0 == 0x81:
        if p + 3 > len(data):
            return None
        length = struct.unpack("<H", data[p + 1 : p + 3])[0]
        p += 3
    elif b0 == 0x82:
        if p + 5 > len(data):
            return None
        length = struct.unpack("<I", data[p + 1 : p + 5])[0]
        p += 5
    elif b0 < 0x80:
        length = b0
        p += 1
    else:
        p += 1
        if p >= len(data):
            return None
        b0 = data[p]
        if b0 == 0x81:
            if p + 3 > len(data):
                return None
            length = struct.unpack("<H", data[p + 1 : p + 3])[0]
            p += 3
        elif b0 < 0x80:
            length = b0
            p += 1
        else:
            return None

    if length <= 0 or p + length > len(data):
        return None

    try:
        return data[p : p + length].decode("utf-8"), p + length
    except Exception:
        return None


def decode_attributed_body(blob: bytes | None) -> str:
    if not blob:
        return ""
    try:
        data = bytes(blob)
    except Exception:
        return ""
    if b"streamtyped" not in data[:16]:
        return ""

    if b"NSString" not in data:
        return ""

    candidates: list[str] = []
    expected_next: int | None = None
    search_from = 0
    while True:
        idx = data.find(b"NSString", search_from)
        if idx == -1:
            break
        parsed = _attributed_string_at(data, idx)
        if parsed is not None:
            text, end = parsed
            if expected_next is not None and idx > expected_next:
                break
            if text:
                candidates.append(text)
            expected_next = end
            search_from = max(idx + 1, end)
        else:
            search_from = idx + len(b"NSString")

    if not candidates:
        return _attributed_fail(data, "no decodable NSString payload")
    if len(candidates) == 1:
        return candidates[0]
    return "".join(candidates)


# ---------------------------------------------------------------------------
# Contacts (AddressBook sqlite)
#
# Keys in the returned dict are *normalized handles*:
#   - phone numbers: the last 10 digits (US-style; strips formatting)
#   - email addresses: lowercased, stripped
# Values are the display name for that contact. First+Last if present,
# otherwise the organization name (so "Café Vivant" still resolves even
# without a person attached).
# ---------------------------------------------------------------------------
_ADDRESSBOOK_PATTERNS = (
    "~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb",
    "~/Library/Application Support/AddressBook/AddressBook-v22.abcddb",
)


def _normalize_handle(h: str) -> str:
    """Produce the contacts-dict key for a handle string.

    Emails normalize to lowercase. Phones normalize to their last 10 digits.
    Anything shorter (short-codes) or unrecognized returns ''.
    """
    if not h:
        return ""
    s = h.strip()
    if "@" in s:
        return s.lower()
    digits = re.sub(r"[^0-9]", "", s)
    return digits[-10:] if len(digits) >= 10 else ""


def load_contacts() -> dict[str, str]:
    """Return {normalized_handle: display_name}.

    Reads every AddressBook-v22.abcddb we can find (the local source + any
    CardDAV/iCloud sources). Loads phones, emails, and organizations. Logs
    how many handles were loaded so debugging doesn't require guessing.
    """
    handle_to_name: dict[str, str] = {}
    db_files: list[str] = []
    for pattern in _ADDRESSBOOK_PATTERNS:
        db_files.extend(glob.glob(os.path.expanduser(pattern)))
    if not db_files:
        log("contacts: no AddressBook-v22.abcddb files found "
            "(checked Sources/* and top-level)")
        return handle_to_name

    total_phones = 0
    total_emails = 0
    for p in db_files:
        try:
            # immutable=1 is a belt-and-suspenders: read-only *and* skip
            # locking, which avoids contending with Contacts.app.
            conn = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
            cur = conn.cursor()

            # 1. Build Z_PK -> display name map. Person records get
            #    "First Last"; company records fall back to ZORGANIZATION.
            records: dict[int, str] = {}
            try:
                cur.execute(
                    "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION "
                    "FROM ZABCDRECORD"
                )
            except sqlite3.Error:
                # Older schema may not have ZORGANIZATION — retry without it.
                cur.execute("SELECT Z_PK, ZFIRSTNAME, ZLASTNAME FROM ZABCDRECORD")
                for pk, fn, ln in cur.fetchall():
                    name = " ".join(x for x in (fn, ln) if x).strip()
                    if name:
                        records[pk] = name
            else:
                for pk, fn, ln, org in cur.fetchall():
                    name = " ".join(x for x in (fn, ln) if x).strip()
                    if not name and org:
                        name = org.strip()
                    if name:
                        records[pk] = name

            # 2. Phone numbers.
            try:
                cur.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER")
                for owner, num in cur.fetchall():
                    if owner not in records or not num:
                        continue
                    digits = re.sub(r"[^0-9]", "", num)
                    if len(digits) >= 10:
                        if handle_to_name.setdefault(digits[-10:], records[owner]) \
                                is records[owner]:
                            total_phones += 1
            except sqlite3.Error as e:
                log(f"contacts: phones table error on {p}: {e}")

            # 3. Email addresses. Prefer the normalized form, fall back to raw.
            try:
                cur.execute(
                    "SELECT ZOWNER, ZADDRESSNORMALIZED, ZADDRESS FROM ZABCDEMAILADDRESS"
                )
                rows = cur.fetchall()
            except sqlite3.Error:
                # Older schema may not have ZADDRESSNORMALIZED.
                try:
                    cur.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS")
                    rows = [(o, None, a) for (o, a) in cur.fetchall()]
                except sqlite3.Error as e:
                    log(f"contacts: emails table error on {p}: {e}")
                    rows = []
            for owner, norm, raw in rows:
                if owner not in records:
                    continue
                addr = (norm or raw or "").strip().lower()
                if addr and "@" in addr:
                    if handle_to_name.setdefault(addr, records[owner]) \
                            is records[owner]:
                        total_emails += 1

            conn.close()
        except Exception as e:
            log(f"contacts: warn on {p}: {e}")

    log(f"contacts: loaded {len(handle_to_name)} handles "
        f"({total_phones} phones, {total_emails} emails) "
        f"from {len(db_files)} source(s)")
    return handle_to_name


def lookup_name(chat_id: str, sender: str, contacts: dict[str, str]) -> str:
    """Resolve the display name for a 1:1 chat or a message sender.

    Tries chat_id and sender in order — one of them is typically the
    canonical iMessage handle (phone or email).
    """
    for candidate in (chat_id, sender):
        key = _normalize_handle(candidate or "")
        if key:
            n = contacts.get(key)
            if n:
                return n
    return ""


def load_chat_participants(
    conn: sqlite3.Connection, chat_rowids: Iterable[int] | None = None
) -> dict[Any, list[str]]:
    """Return participant handles keyed by chat identifier or row ID.

    Used to build a human label for group chats whose chat_identifier is
    just "chatNNNNN…" and whose display_name is empty. With participants
    in hand we can render e.g. "Alice, Bob & 2 others" instead of the
    opaque group id. `chat_rowids` restricts the scan to candidate chats
    (bounded callers such as list_chats) and returns a ROWID-keyed map so
    duplicate chat_identifier rows cannot cross-contaminate participants.
    None keeps today's full scan and identifier-keyed map for review.
    """
    cur = conn.cursor()
    sql = """
        SELECT c.ROWID, c.chat_identifier, h.id
        FROM chat c
        JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
        JOIN handle h ON h.ROWID = chj.handle_id
        """
    if chat_rowids is None:
        cur.execute(sql)
    else:
        ids = sorted({int(r) for r in chat_rowids})
        if not ids:
            return defaultdict(list)
        # SQLite's default variable limit is 999; chunk to stay under it.
        rows: list = []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            cur.execute(sql + " WHERE c.ROWID IN (%s)" % ",".join("?" * len(chunk)), chunk)
            rows.extend(cur.fetchall())
        out: dict[str, list[str]] = defaultdict(list)
        for chat_rowid, _chat_ident, handle_id in rows:
            hi = handle_id.decode("utf-8", "ignore") if isinstance(handle_id, bytes) else (handle_id or "")
            if chat_rowid is not None and hi:
                out[int(chat_rowid)].append(hi)
        return out
    out: dict[str, list[str]] = defaultdict(list)
    for _chat_rowid, chat_ident, handle_id in cur.fetchall():
        ci = chat_ident.decode("utf-8", "ignore") if isinstance(chat_ident, bytes) else (chat_ident or "")
        hi = handle_id.decode("utf-8", "ignore") if isinstance(handle_id, bytes) else (handle_id or "")
        if ci and hi:
            out[ci].append(hi)
    return out


def group_label(participants: list[str], contacts: dict[str, str]) -> str:
    """Render a friendly group-chat label from a list of handles.

    Uses first names when a contact resolves; falls back to the last 4
    digits of a phone ("…4567") or the raw email otherwise. Caps at 3
    named participants with "& N others" suffix so the label fits on
    one line in the review bucket.
    """
    if not participants:
        return ""
    parts: list[str] = []
    for h in participants:
        name = lookup_name(h, h, contacts)
        if name:
            parts.append(name.split()[0])  # first name only
        elif "@" in (h or ""):
            parts.append(h)
        else:
            d = re.sub(r"[^0-9]", "", h or "")
            parts.append(f"…{d[-4:]}" if len(d) >= 4 else h)
    if len(parts) <= 3:
        return ", ".join(parts)
    return ", ".join(parts[:3]) + f" & {len(parts) - 3} others"


# ---------------------------------------------------------------------------
# Read privacy policy
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PrivacyPolicy:
    mode: str
    blocklist: tuple[str, ...]
    allowlist: tuple[str, ...]


def _load_list(path: Path, require_root_owner: bool = False, require_uid_owner: bool = False) -> tuple[str, ...]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return ()
    if not stat.S_ISREG(metadata.st_mode):
        log(f"privacy policy rejected: {path} must be a regular file")
        return ()
    if require_root_owner:
        if metadata.st_uid != 0:
            log(f"privacy policy rejected: {path} must be root-owned")
            return ()
        if metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            log(f"privacy policy rejected: {path} has group/world permissions")
            return ()
    if require_uid_owner:
        if metadata.st_uid != os.getuid():
            log(f"privacy policy rejected: {path} must be owned by the current user (uid {os.getuid()})")
            return ()
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            log(f"privacy policy rejected: {path} must not be group/world-writable")
            return ()
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return tuple(out)


def load_privacy_policy() -> PrivacyPolicy:
    # Manager role: no policy files loaded
    if bridge_role() == "manager":
        return PrivacyPolicy(mode="blocklist", blocklist=(), allowlist=())

    mode_override = os.environ.get("COWORK_IMESSAGE_READ_POLICY", "runtime")
    if mode_override in ("allowlist", "blocklist"):
        mode = mode_override
    else:
        # Product mode: apply permission check to read_policy.txt
        can_read_policy = True
        if WRAPPER_MODE == "product":
            try:
                metadata = READ_POLICY_PATH.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    log(f"read_policy.txt rejected: must be a regular file")
                    can_read_policy = False
                elif metadata.st_uid != os.getuid():
                    log(f"read_policy.txt rejected: must be owned by current user (uid {os.getuid()})")
                    can_read_policy = False
                elif metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    log(f"read_policy.txt rejected: must not be group/world-writable")
                    can_read_policy = False
            except FileNotFoundError:
                can_read_policy = False

        if can_read_policy:
            try:
                mode = READ_POLICY_PATH.read_text(encoding="utf-8").strip().lower()
            except FileNotFoundError:
                # Product mode: missing read_policy.txt defaults to allowlist (fail closed)
                if WRAPPER_MODE == "product":
                    mode = "allowlist"
                else:
                    mode = "blocklist"
        else:
            # Product mode: permission check failed, treat as missing → allowlist (fail closed)
            if WRAPPER_MODE == "product":
                mode = "allowlist"
            else:
                mode = "blocklist"

        if mode not in ("allowlist", "blocklist"):
            log(f"invalid read policy {mode!r}; failing closed in allowlist mode")
            mode = "allowlist"

    require_root = os.environ.get("COWORK_IMESSAGE_REQUIRE_ROOT_POLICY") == "1"
    # Product mode: policy files must be uid-owned and not group/world-writable
    require_uid = WRAPPER_MODE == "product"
    return PrivacyPolicy(
        mode=mode,
        blocklist=_load_list(BLOCKLIST_PATH, require_uid_owner=require_uid),
        allowlist=_load_list(ALLOWLIST_PATH, require_root_owner=require_root, require_uid_owner=require_uid),
    )


def load_blocklist() -> list[str]:
    """Backward-compatible loader retained for existing integrations/tests."""
    return list(_load_list(BLOCKLIST_PATH))


def is_send_policy_enabled() -> bool:
    """Check if send_policy.json enables sending. Product mode only; DIY always returns True."""
    if WRAPPER_MODE != "product":
        return True
    try:
        metadata = SEND_POLICY_PATH.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            log(f"send_policy.json rejected: must be a regular file")
            return False
        if metadata.st_uid != os.getuid():
            log(f"send_policy.json rejected: must be owned by current user (uid {os.getuid()})")
            return False
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            log(f"send_policy.json rejected: must not be group/world-writable")
            return False
        policy = json.loads(SEND_POLICY_PATH.read_text(encoding="utf-8"))
        if not isinstance(policy, dict):
            log(f"send_policy.json malformed: root must be an object")
            return False
        return policy.get("enabled") is True
    except FileNotFoundError:
        return False
    except (json.JSONDecodeError, OSError) as e:
        log(f"send_policy.json rejected: {e}")
        return False


def _coerce_policy(policy: PrivacyPolicy | list[str]) -> PrivacyPolicy:
    if isinstance(policy, PrivacyPolicy):
        return policy
    return PrivacyPolicy(mode="blocklist", blocklist=tuple(policy), allowlist=())


def _matches_list(chat_id: str, sender: str, entries: tuple[str, ...] | list[str]) -> bool:
    if not entries:
        return False
    cid = chat_id or ""
    snd = sender or ""
    cid_l10 = _last10(cid)
    snd_l10 = _last10(snd)
    for entry in entries:
        entry_l10 = _last10(entry)
        if entry_l10 and (entry_l10 == cid_l10 or entry_l10 == snd_l10):
            return True
        if not entry_l10:
            lowered = entry.lower()
            if "@" in entry and (lowered == cid.lower() or lowered == snd.lower()):
                return True
            if "@" not in entry and (lowered in cid.lower() or lowered in snd.lower()):
                return True
    return False


def _last10(s: str) -> str:
    d = re.sub(r"[^0-9]", "", s or "")
    return d[-10:] if len(d) >= 10 else ""


def is_blocked(chat_id: str, sender: str, policy: PrivacyPolicy | list[str]) -> bool:
    return _matches_list(chat_id, sender, _coerce_policy(policy).blocklist)


def is_read_allowed(chat_id: str, sender: str, policy: PrivacyPolicy | list[str]) -> bool:
    resolved = _coerce_policy(policy)
    if is_blocked(chat_id, sender, resolved):
        return False
    if resolved.mode == "allowlist":
        return bool(_matches_list(chat_id, sender, resolved.allowlist))
    return True


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
_CODE_NEAR_WORD = re.compile(
    r"(?:\b(?:code|verification|OTP|passcode|one[- ]time)\b[^0-9]{0,20}\b(\d{4,8})\b)"
    r"|(?:\b(\d{4,8})\b[^0-9]{0,20}\b(?:code|verification|OTP|passcode)\b)",
    re.IGNORECASE,
)
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def redact(text: str) -> str:
    if not text:
        return text
    text = _CODE_NEAR_WORD.sub("[REDACTED-2FA]", text)
    text = _CARD_RE.sub(lambda m: "[REDACTED-CARD]" if len(re.sub(r"\D", "", m.group(0))) >= 13 else m.group(0), text)
    text = _SSN_RE.sub("[REDACTED-SSN]", text)
    return text


# ---------------------------------------------------------------------------
# Automated / low-signal filters (for the review action)
# ---------------------------------------------------------------------------
_AUTO_PATTERNS = re.compile(
    "|".join(
        [
            r"lyft:.*(requested|on their way|arrived|cancelled)",
            r"uber:.*(on|arriving|trip)",
            r"your .*verification code",
            r"your .*code is",
            r"verification code|one-time password|\botp\b",
            r"actblue|midterms|reelection|\bdonate\b|rush \$\d+",
            r"stop to quit|reply stop",
            r"error invalid number",
            r"delivered|out for delivery|package|shipment",
            r"check-in",
            r"bill is ready|statement is available",
            r"your appointment|appointment reminder",
        ]
    ),
    re.IGNORECASE,
)
_SHORT_CODE = re.compile(r"^[+]?[0-9]{3,6}$")
_REACTION_PREFIX = re.compile(
    r"^(liked|loved|laughed at|emphasized|questioned|disliked|reacted|removed a)"
    r"( a| an)? [“\"'\ufffc]",
    re.IGNORECASE,
)
_ONE_WORD_ACK = {
    "thx", "thanks", "ty", "ok", "okay", "k", "sure", "sounds good",
    "sounds good!", "for sure", "cool", "nice", "great", "got it",
    "yep", "yup", "nope",
}


def is_automated(chat_id: str, text: str) -> bool:
    if _SHORT_CODE.match(chat_id or ""):
        return True
    if "rbm.goog" in (chat_id or ""):
        return True
    if not text:
        return False
    return bool(_AUTO_PATTERNS.search(text))


def is_low_signal(text: str) -> bool:
    if not text:
        return True
    t = text.strip()
    if _REACTION_PREFIX.match(t):
        return True
    if t.lower().rstrip("!. ") in _ONE_WORD_ACK:
        return True
    return False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _as_number(v: Any, name: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) and not (isinstance(v, str) and v.strip()):
        raise ValueError(f"{name} must be a number")
    try:
        return float(v)
    except Exception:
        raise ValueError(f"{name} must be a number")


def validate_days(v: Any) -> float:
    n = _as_number(v, "days")
    if n <= 0 or n > MAX_DAYS:
        raise ValueError(f"days must be in (0, {MAX_DAYS}]")
    return n


def validate_hours(v: Any) -> float:
    n = _as_number(v, "hours")
    if n <= 0 or n > MAX_HOURS:
        raise ValueError(f"hours must be in (0, {MAX_HOURS}]")
    return n


def validate_limit(v: Any) -> int:
    n = int(_as_number(v, "limit"))
    if n <= 0 or n > MAX_LIMIT:
        raise ValueError(f"limit must be in (0, {MAX_LIMIT}]")
    return n


def validate_search(v: Any) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ValueError("search term required")
    if len(v) > MAX_SEARCH_LEN:
        raise ValueError("search term too long")
    return v


def validate_list_chats_days(v: Any) -> float:
    n = _as_number(v, "days")
    if n <= 0 or n > MAX_LIST_CHATS_DAYS:
        raise ValueError(f"days must be in (0, {MAX_LIST_CHATS_DAYS}]")
    return n


def validate_list_chats_limit(v: Any) -> int:
    """`limit` is an integer in the protocol; reject fractional values."""
    if isinstance(v, bool):
        raise ValueError("limit must be an integer")
    if isinstance(v, float):
        if not v.is_integer():
            raise ValueError("limit must be an integer")
        v = int(v)
    if isinstance(v, str):
        try:
            v = int(v.strip())
        except ValueError:
            raise ValueError("limit must be an integer")
    return validate_limit(v)


def validate_list_chats_query(v: Any) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError("query must be a string")
    if len(v) > MAX_LIST_CHATS_QUERY_LEN:
        raise ValueError("query too long")
    q = v.strip()
    return q or None


def validate_bool(v: Any, name: str, default: bool) -> bool:
    if v is None:
        return default
    if not isinstance(v, bool):
        raise ValueError(f"{name} must be a boolean")
    return v


_EMAIL_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_EMAIL_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_EMAIL_RE = re.compile(
    rf"^{_EMAIL_ATOM}(?:\.{_EMAIL_ATOM})*@"
    rf"{_EMAIL_LABEL}(?:\.{_EMAIL_LABEL})+$"
)
_PHONE_RE = re.compile(r"^[+0-9().\- ]+$")


def validate_chat(v: Any) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ValueError("chat identifier required")
    if len(v) > 200:
        raise ValueError("chat identifier too long")
    return v.strip()


def validate_send_recipient(v: Any) -> str:
    """Validate a send target. Accepts only phone numbers and email addresses.
    Rejects all group chat IDs (any 'chat' prefix).
    """
    identifier = validate_chat(v)

    if identifier.casefold().startswith("chat"):
        raise ValueError("group chat IDs are not supported for sending; use a phone number or email")

    if "@" in identifier:
        if not _EMAIL_RE.fullmatch(identifier):
            raise ValueError("send recipient must be a valid email address or phone number")
        return identifier.strip().lower()

    digits = re.sub(r"\D", "", identifier)
    if len(digits) >= 10 and _PHONE_RE.fullmatch(identifier):
        return identifier.strip()

    raise ValueError("send recipient must be a valid phone number or email address")


def validate_send_text(v: Any) -> str:
    """Bounds-check a message body for outbound send.

    Allows printable Unicode (including emoji) plus \\n, \\r, \\t. Rejects
    other C0 control characters to avoid exotic payloads being relayed
    through Messages.app.
    """
    if not isinstance(v, str):
        raise ValueError("text must be a string")
    if not v:
        raise ValueError("text cannot be empty")
    if len(v) > MAX_SEND_LEN:
        raise ValueError(f"text too long ({len(v)} chars; max {MAX_SEND_LEN})")
    for ch in v:
        if ord(ch) < 0x20 and ch not in ("\n", "\r", "\t"):
            raise ValueError(
                f"text contains disallowed control character U+{ord(ch):04X}"
            )
    return v


def validate_service(v: Any) -> str:
    """Normalize the send service. Defaults to iMessage when omitted."""
    if v is None:
        return "iMessage"
    if v not in _SERVICE_ENUM:
        raise ValueError(
            f"service must be one of {_SERVICE_ENUM}, got {v!r}"
        )
    return v


# ---------------------------------------------------------------------------
# AppleScript shellout (send path only)
# ---------------------------------------------------------------------------
def _escape_as_string(s: str) -> str:
    """Escape a Python string for embedding as an AppleScript string literal.

    AppleScript string literals are double-quoted; only `"` and `\\` need
    to be escaped. We do NOT try to escape arbitrary message bodies this
    way — those are handed to AppleScript via a tempfile to sidestep the
    whole class of escaping bugs. This helper is for short, already-
    validated fields like the recipient identifier and the tempfile path.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _run_osascript(script: str, timeout: float = OSASCRIPT_TIMEOUT_S
                   ) -> tuple[int, str, str]:
    """Run `script` via osascript (fed on stdin). Returns (rc, stdout, stderr).

    Separated out from the action functions so tests can monkeypatch it.
    """
    r = subprocess.run(
        ["/usr/bin/osascript", "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def _run_send_confirmation(
    *, to: str, resolved_name: str, service: str, text: str
) -> bool:
    """Show the full outbound payload in the native confirmation helper.

    Return True only for the helper's explicit Send exit status. Cancel and
    timeout return False; malformed input or helper failures raise.
    """
    payload = json.dumps(
        {
            "client_name": HOST_DISPLAY_NAME,
            "to": to,
            "resolved_name": resolved_name,
            "service": service,
            "text": text,
        },
        ensure_ascii=False,
    )
    try:
        result = subprocess.run(
            [str(CONFIRM_HELPER_PATH)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=70,
        )
    except subprocess.TimeoutExpired:
        return False
    except OSError as e:
        raise RuntimeError(f"confirmation helper could not start: {e}") from e

    if result.returncode == 0:
        return True
    if result.returncode in (1, 3):
        return False
    detail = (result.stderr or result.stdout or "no output").strip()
    raise RuntimeError(
        f"confirmation helper failed (rc={result.returncode}): {detail}"
    )


# ---------------------------------------------------------------------------
# DB handling
# ---------------------------------------------------------------------------
def copy_chatdb() -> Path:
    if not CHAT_DB_PATH.exists():
        raise RuntimeError(f"chat.db not found at {CHAT_DB_PATH}")
    fd, tmp = tempfile.mkstemp(prefix="cowork_imessage_", suffix=".db")
    os.close(fd)
    snapshot = Path(tmp)
    source = None
    destination = None
    try:
        # chat.db is live and may contain uncheckpointed WAL rows. Do not use
        # immutable=1 here: it asserts the file cannot change and disables
        # locking/change detection. The online backup API supplies the snapshot.
        source_uri = f"{CHAT_DB_PATH.resolve().as_uri()}?mode=ro&cache=private"
        source = sqlite3.connect(source_uri, uri=True, timeout=5)
        destination = sqlite3.connect(str(snapshot))
        source.backup(destination)
        return snapshot
    except Exception:
        cleanup_tmpdb(snapshot)
        raise
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()


def cleanup_tmpdb(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass


def open_snapshot(db_path: Path) -> sqlite3.Connection:
    """Open a completed, private chat.db snapshot without SQLite sidecars."""
    snapshot_uri = f"{db_path.resolve().as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(snapshot_uri, uri=True)
    conn.text_factory = bytes
    return conn


def to_apple_ns(unix_seconds: float) -> int:
    return int((unix_seconds - APPLE_EPOCH) * 1_000_000_000)


def from_apple_ns(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1_000_000_000 + APPLE_EPOCH)


def fetch_messages(
    conn: sqlite3.Connection,
    cutoff_ns: int,
    *,
    search: str | None = None,
    chat_filter_substr: str | None = None,
) -> list[dict]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.chat_identifier,
               COALESCE(c.display_name, ''),
               m.date,
               m.is_from_me,
               COALESCE(h.id, ''),
               m.text,
               m.attributedBody
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        JOIN chat c ON c.ROWID = cmj.chat_id
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE m.date > ?
        ORDER BY c.chat_identifier, m.date ASC
        """,
        (cutoff_ns,),
    )
    out: list[dict] = []
    for row in cur.fetchall():
        chat_id = (row[0] or b"").decode("utf-8", "ignore")
        disp = (row[1] or b"").decode("utf-8", "ignore")
        ts_ns = row[2]
        is_me = bool(row[3])
        sender = (row[4] or b"").decode("utf-8", "ignore")
        raw_text = row[5]
        attrib = row[6]
        text = raw_text.decode("utf-8", "ignore") if raw_text else ""
        if not text and attrib:
            text = decode_attributed_body(attrib)

        if chat_filter_substr and chat_filter_substr.lower() not in chat_id.lower() \
                and chat_filter_substr.lower() not in sender.lower():
            continue
        if search and search.lower() not in text.lower():
            continue

        out.append(
            {
                "chat_id": chat_id,
                "display_name": disp,
                "ts_ns": ts_ns,
                "ts": from_apple_ns(ts_ns).isoformat(timespec="seconds"),
                "is_from_me": is_me,
                "sender": sender,
                "text": text,
            }
        )
    return out


def apply_read_policy(
    msgs: list[dict], policy: PrivacyPolicy | list[str]
) -> list[dict]:
    return [m for m in msgs if is_read_allowed(m["chat_id"], m["sender"], policy)]


def apply_blocklist(msgs: list[dict], blocklist: list[str]) -> list[dict]:
    """Backward-compatible alias for blocklist-only callers."""
    return apply_read_policy(msgs, blocklist)


def filter_contacts(
    contacts: dict[str, str], policy: PrivacyPolicy | list[str]
) -> dict[str, str]:
    return {
        handle: name
        for handle, name in contacts.items()
        if is_read_allowed(handle, handle, policy)
    }


# ---------------------------------------------------------------------------
# Chat resolution: "Angel Vossough" | phone | email -> chat_identifier substring
# ---------------------------------------------------------------------------
def resolve_chat_filter(q: str, contacts: dict[str, str]) -> str:
    """Return a substring suitable for matching chat_identifier/sender."""
    digits = re.sub(r"[^0-9]", "", q)
    if len(digits) >= 10:
        return digits[-10:]
    if "@" in q:
        return q
    # Treat as a contact-name query.
    ql = q.lower().strip()
    for d10, name in contacts.items():
        if ql in name.lower():
            return d10
    # No match — fall through to raw substring match, which usually fails.
    return q


# ---------------------------------------------------------------------------
# Classification (review action)
# ---------------------------------------------------------------------------
def classify_chats(
    msgs: list[dict],
    contacts: dict[str, str],
    participants: dict[str, list[str]] | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    participants = participants or {}
    chats: dict[str, list[dict]] = defaultdict(list)
    for m in msgs:
        chats[m["chat_id"]].append(m)

    needs_reply: list[dict] = []
    low_priority: list[dict] = []
    skip: list[dict] = []

    for chat_id, ms in chats.items():
        ms.sort(key=lambda x: x["ts_ns"])
        last = ms[-1]
        if last["is_from_me"]:
            continue  # already replied

        last_text = last["text"] or ""
        display = last.get("display_name") or ""
        # Distinguish 1:1 vs group. For 1:1 the chat_identifier is the
        # handle itself (phone or email); for groups it's "chatNNNNN".
        is_group = chat_id.startswith("chat") and not re.fullmatch(r"[+0-9@.]+", chat_id)
        if is_group:
            # Always label groups by group name / participants, never by
            # the last sender — otherwise a 5-person thread looks like a
            # 1:1 with whoever just happened to speak last.
            contact_name = ""
            if not display:
                display = group_label(participants.get(chat_id, []), contacts)
        else:
            contact_name = lookup_name(chat_id, last["sender"], contacts)
        label = contact_name or display or chat_id

        automated_last = is_automated(chat_id, last_text)
        has_human = any(
            not m["is_from_me"] and not is_automated(chat_id, m.get("text", ""))
            for m in ms
        )

        entry = {
            "chat_id": chat_id,
            "label": label,
            "contact_name": contact_name,
            "display_name": display,
            "last_ts": last["ts"],
            "last_text": redact(last_text)[:MAX_TEXT_SNIPPET],
            "context": [
                {
                    "ts": m["ts"],
                    "me": m["is_from_me"],
                    "text": redact(m.get("text", "") or "")[:400],
                }
                for m in ms[-MAX_CONTEXT_MESSAGES:]
            ],
            "msg_count": len(ms),
        }

        if automated_last and not has_human:
            skip.append(entry)
        elif is_low_signal(last_text):
            low_priority.append(entry)
        else:
            needs_reply.append(entry)

    for bucket in (needs_reply, low_priority, skip):
        bucket.sort(key=lambda x: x["last_ts"], reverse=True)
    return needs_reply, low_priority, skip


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
def action_review(params, conn, contacts, privacy_policy):
    days = validate_days(params.get("days", 2))
    cutoff_ns = to_apple_ns(time.time() - days * 86400)
    msgs = fetch_messages(conn, cutoff_ns)
    msgs = apply_read_policy(msgs, privacy_policy)
    participants = load_chat_participants(conn)
    needs_reply, low_priority, skip = classify_chats(msgs, contacts, participants)
    return {
        "days": days,
        "counts": {
            "needs_reply": len(needs_reply),
            "low_priority": len(low_priority),
            "skip": len(skip),
            "total_messages": len(msgs),
        },
        "needs_reply": needs_reply,
        "low_priority": low_priority,
        # Skip bucket summary only — don't ship 2FA codes and Uber updates
        # into the agent context.
        "skip_summary": [
            {"chat_id": e["chat_id"], "label": e["label"], "last_ts": e["last_ts"]}
            for e in skip[:20]
        ],
    }


def action_search(params, conn, contacts, privacy_policy):
    term = validate_search(params.get("term"))
    days = validate_days(params.get("days", 30))
    limit = validate_limit(params.get("limit", 100))
    cutoff_ns = to_apple_ns(time.time() - days * 86400)
    msgs = fetch_messages(conn, cutoff_ns, search=term)
    msgs = apply_read_policy(msgs, privacy_policy)
    # Sort descending by timestamp so newest matches come first.
    msgs.sort(key=lambda x: x["ts_ns"], reverse=True)
    matches = []
    for m in msgs[:limit]:
        name = lookup_name(m["chat_id"], m["sender"], contacts)
        matches.append(
            {
                "chat_id": m["chat_id"],
                "contact_name": name,
                "ts": m["ts"],
                "is_from_me": m["is_from_me"],
                "text": redact(m["text"])[:MAX_TEXT_SNIPPET],
            }
        )
    return {"term": term, "days": days, "match_count": len(matches), "matches": matches}


def action_chat_history(params, conn, contacts, privacy_policy):
    chat_q = validate_chat(params.get("chat"))
    days = validate_days(params.get("days", 14))
    limit = validate_limit(params.get("limit", 100))
    visible_contacts = filter_contacts(contacts, privacy_policy)
    substr = resolve_chat_filter(chat_q, visible_contacts)
    cutoff_ns = to_apple_ns(time.time() - days * 86400)
    msgs = fetch_messages(conn, cutoff_ns, chat_filter_substr=substr)
    msgs = apply_read_policy(msgs, privacy_policy)
    msgs.sort(key=lambda x: x["ts_ns"])
    msgs = msgs[-limit:]
    out = []
    for m in msgs:
        name = lookup_name(m["chat_id"], m["sender"], visible_contacts)
        out.append(
            {
                "chat_id": m["chat_id"],
                "contact_name": name,
                "ts": m["ts"],
                "is_from_me": m["is_from_me"],
                "text": redact(m["text"])[:MAX_TEXT_SNIPPET],
            }
        )
    return {"chat_query": chat_q, "resolved_substr": substr, "count": len(out), "messages": out}


def action_response_stats(params, conn, contacts, privacy_policy):
    chat_q = validate_chat(params.get("chat"))
    hours = validate_hours(params.get("hours", 24))
    substr = resolve_chat_filter(chat_q, filter_contacts(contacts, privacy_policy))
    cutoff_ns = to_apple_ns(time.time() - hours * 3600)
    msgs = fetch_messages(conn, cutoff_ns, chat_filter_substr=substr)
    msgs = apply_read_policy(msgs, privacy_policy)
    msgs.sort(key=lambda x: x["ts_ns"])

    deltas: list[float] = []
    pending_them: dict | None = None
    for m in msgs:
        if not m["is_from_me"]:
            # First inbound in a run; later inbounds don't reset the clock.
            if pending_them is None:
                pending_them = m
        else:
            if pending_them is not None:
                dt = (m["ts_ns"] - pending_them["ts_ns"]) / 1_000_000_000
                if dt >= 0:
                    deltas.append(dt)
                pending_them = None

    def fmt(sec: float | None) -> str | None:
        if sec is None:
            return None
        if sec < 60:
            return f"{sec:.0f}s"
        if sec < 3600:
            return f"{sec / 60:.1f}m"
        if sec < 86400:
            return f"{sec / 3600:.2f}h"
        return f"{sec / 86400:.2f}d"

    avg = sum(deltas) / len(deltas) if deltas else None
    return {
        "chat_query": chat_q,
        "resolved_substr": substr,
        "hours": hours,
        "sample_size": len(deltas),
        "avg_seconds": avg,
        "avg_human": fmt(avg),
        "median_seconds": sorted(deltas)[len(deltas) // 2] if deltas else None,
        "min_seconds": min(deltas) if deltas else None,
        "max_seconds": max(deltas) if deltas else None,
        "total_inbound_messages": sum(1 for m in msgs if not m["is_from_me"]),
        "total_outbound_messages": sum(1 for m in msgs if m["is_from_me"]),
    }


def action_contacts_lookup(params, conn, contacts, privacy_policy):
    name = params.get("name", "")
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValueError("name must be a 1..100 char string")
    nl = name.lower()
    matches = []
    for handle, full_name in contacts.items():
        # Manager role: unfiltered contacts
        if bridge_role() != "manager" and not is_read_allowed(handle, handle, privacy_policy):
            continue
        if nl in full_name.lower():
            if "@" in handle:
                matches.append({"name": full_name, "email": handle})
            else:
                matches.append({"name": full_name, "phone_last10": handle})
    return {"query": name, "match_count": len(matches), "matches": matches[:25]}


# chat.style in chat.db is IMChatStyle: 43 (ASCII '+') = group chat,
# 45 (ASCII '-') = one-to-one "instant message" chat. Same mapping as
# ENGINEERING_PLAN §2.4 and the review classifier's chat-id heuristic.
_CHAT_STYLE_GROUP = 43
_CHAT_STYLE_DIRECT = 45


def _chat_kind(chat_id: str, style: Any) -> str:
    if style == _CHAT_STYLE_GROUP:
        return "group"
    if style == _CHAT_STYLE_DIRECT:
        return "direct"
    # Fallback heuristic, same rule classify_chats uses.
    is_group = chat_id.startswith("chat") and not re.fullmatch(r"[+0-9@.]+", chat_id)
    return "group" if is_group else "direct"


def _decode_db_text(v: Any) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8", "ignore")
    return v or ""


def action_list_chats(params, conn, contacts, privacy_policy):
    """Enumerate threads with recent activity, without any message content.

    Management-bridge only (see ROLE_ACTIONS). Intended for policy discovery:
    the app shows this list so the user can build an allowlist/blocklist. The
    query deliberately never selects `message.text` or `message.attributedBody`
    and the read policy is not applied — the point is to see which threads
    exist so a policy can be written about them.
    """
    days = validate_list_chats_days(params.get("days", LIST_CHATS_DEFAULT_DAYS))
    limit = validate_list_chats_limit(params.get("limit", LIST_CHATS_DEFAULT_LIMIT))
    include_groups = validate_bool(params.get("include_groups"), "include_groups", True)
    query = validate_list_chats_query(params.get("query"))
    cutoff_ns = to_apple_ns(time.time() - days * 86400)

    cur = conn.cursor()
    sql = """
        SELECT c.ROWID,
               c.chat_identifier,
               COALESCE(c.display_name, ''),
               COALESCE(c.service_name, ''),
               c.style,
               COUNT(m.ROWID),
               MAX(m.date)
        FROM chat c
        JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
        JOIN message m ON m.ROWID = cmj.message_id
        WHERE m.date > ?
        GROUP BY c.ROWID
        ORDER BY MAX(m.date) DESC
        """
    post_filtering = query is not None or not include_groups
    candidate_limit = limit if not post_filtering else max(limit + 1, LIST_CHATS_FILTER_SCAN_LIMIT)
    if not post_filtering:
        # No post-filtering: let SQLite stop after limit+1 rows so we can
        # report truncation without pulling every chat.
        cur.execute(sql + " LIMIT ?", (cutoff_ns, limit + 1))
    else:
        # Query/group filters happen after participant labels are assembled.
        # Bound the candidate scan anyway so filtered requests cannot walk a
        # whole large chat database before returning no matches.
        cur.execute(sql + " LIMIT ?", (cutoff_ns, candidate_limit + 1))
    rows = cur.fetchall()
    candidate_truncated = len(rows) > candidate_limit
    rows = rows[:candidate_limit]

    # Participants only for the candidate chats (bounded by LIMIT above), not
    # a full chat_handle_join scan.
    participants_by_chat = load_chat_participants(conn, (row[0] for row in rows))
    ql = query.lower() if query else None
    items: list[dict] = []
    for row in rows:
        chat_rowid = int(row[0])
        chat_id = _decode_db_text(row[1])
        display = _decode_db_text(row[2])
        service = _decode_db_text(row[3])
        style = row[4]
        message_count = int(row[5] or 0)
        last_ns = row[6]
        if not chat_id:
            continue
        kind = _chat_kind(chat_id, style)
        if kind == "group" and not include_groups:
            continue
        participants = list(participants_by_chat.get(chat_rowid, []))
        if kind == "direct" and not participants:
            participants = [chat_id]
        if kind == "group":
            label = display or group_label(participants, contacts) or chat_id
        else:
            label = lookup_name(chat_id, chat_id, contacts) or display or chat_id
        if ql is not None:
            haystack = [label.lower(), display.lower(), chat_id.lower()]
            haystack.extend(p.lower() for p in participants)
            if not any(ql in h for h in haystack):
                continue
        items.append(
            {
                "chat_id": chat_id,
                "kind": kind,
                "display_name": display,
                "label": label,
                "participants": participants[:MAX_LIST_CHATS_PARTICIPANTS],
                "participant_count": len(participants),
                "service": service,
                "message_count": message_count,
                "last_activity_date": (
                    from_apple_ns(last_ns).date().isoformat() if last_ns is not None else None
                ),
            }
        )
        if len(items) > limit:
            break

    truncated = len(items) > limit or candidate_truncated
    items = items[:limit]
    return {
        "window_days": days,
        "chat_count": len(items),
        "truncated": truncated,
        "chats": items,
    }


def action_status(params, conn, contacts, privacy_policy):
    """Return compatibility and local-install status without reading messages."""
    policy = _coerce_policy(privacy_policy)
    return {
        "helper_version": HELPER_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "bridge_role": bridge_role(),
        "allowed_actions": sorted(allowed_actions()),
        "product_id": PRODUCT_ID,
        "wrapper_mode": WRAPPER_MODE,
        "host_display_name": HOST_DISPLAY_NAME,
        "launchd_label": "com.jeffhuber.grokbot-imessage" if WRAPPER_MODE == "baked" else None,
        "confirmation_helper_path": str(CONFIRM_HELPER_PATH),
        "code_root": str(CODE_ROOT),
        "bridge_root": str(BRIDGE_ROOT),
        "policy_dir": str(POLICY_ROOT),
        "install_root": str(CODE_ROOT),
        "python_version": sys.version.split()[0],
        "read_policy": {
            "mode": policy.mode,
            "allowlist_entries": len(policy.allowlist),
            "blocklist_entries": len(policy.blocklist),
            "root_owned_required": os.environ.get("COWORK_IMESSAGE_REQUIRE_ROOT_POLICY") == "1",
        },
        "checks": {
            "chat_db_exists": CHAT_DB_PATH.is_file(),
            "chat_db_readable": os.access(CHAT_DB_PATH, os.R_OK),
            "confirmation_helper_exists": CONFIRM_HELPER_PATH.is_file(),
            "confirmation_helper_executable": os.access(CONFIRM_HELPER_PATH, os.X_OK),
            "requests_dir_exists": REQUESTS_DIR.is_dir(),
            "responses_dir_exists": RESPONSES_DIR.is_dir(),
        },
    }


action_status.needs_db = False  # type: ignore[attr-defined]
action_status.needs_contacts = False  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Send actions (AppleScript-driven)
# ---------------------------------------------------------------------------
def _resolve_contact_name(to: str, contacts: dict[str, str]) -> str:
    """Best-effort name lookup from phone or email targets.

    Group-chat IDs return "" — not because we couldn't look
    them up, but because the Contacts-side loader keys on normalized
    phone numbers and emails only.
    """
    key = _normalize_handle(to)
    return contacts.get(key, "") if key else ""


def action_send_preview(params, conn, contacts, privacy_policy):
    """Non-destructive: resolve the recipient and return what *would* be sent.

    Intended flow — the agent calls `send_preview` first, shows the preview
    to the user (including any contact-name resolution and a "blocked"
    flag if the target is in the blocklist), gets explicit confirmation,
    then calls `send` with the same params AND echoes back the `send_nonce`
    we mint here. v0.4.0+: the nonce is bound to the exact previewed
    payload and expires after SEND_NONCE_TTL seconds, so a forged `send`
    that skips preview — or swaps the body between preview and send — is
    rejected helper-side.
    """
    if not is_send_policy_enabled():
        raise ValueError("send operations are disabled by policy")

    to = validate_send_recipient(params.get("to"))
    text = validate_send_text(params.get("text"))
    service = validate_service(params.get("service"))

    send_nonce = mint_send_nonce(to, text, service)

    resolved_name = (
        _resolve_contact_name(to, contacts)
        if is_read_allowed(to, to, privacy_policy)
        else ""
    )
    return {
        "preview": {
            "to": to,
            "resolved_name": resolved_name,
            "service": service,
            "text": text,
            "text_length": len(text),
            "blocked": is_blocked(to, to, privacy_policy),
        },
        "send_nonce": send_nonce,
        "send_nonce_ttl_seconds": SEND_NONCE_TTL,
    }


action_send_preview.needs_db = False  # type: ignore[attr-defined]


def action_send(params, conn, contacts, privacy_policy):
    """Send an iMessage (or SMS via iPhone relay) via AppleScript.

    The message body is written to a tempfile and read by AppleScript as
    UTF-8, which sidesteps every AppleScript string-escape bug and lets us
    send arbitrary Unicode (including emoji and newlines) unchanged.

    Recipient identifiers are escaped inline as AppleScript string literals
    because they've already passed `validate_send_recipient` (≤200 chars, stripped).

    The `service type` slot is an AppleScript enum, not a string. We pick
    the clause statically from the validated service name so no untrusted
    input is ever interpolated into that slot.
    """
    if not is_send_policy_enabled():
        raise ValueError("send operations are disabled by policy")

    to = validate_send_recipient(params.get("to"))
    text = validate_send_text(params.get("text"))
    service = validate_service(params.get("service"))

    if is_blocked(to, to, privacy_policy):
        raise ValueError(
            f"refusing to send: {to!r} is in contacts/blocked_chats.txt"
        )

    # v0.4.0+: helper-side send gate. `send_preview` must have been called
    # first for this exact (to, text, service) triple, and the resulting
    # single-use nonce must be echoed back within the TTL window. This
    # enforces preview-then-confirm even if the bridge has been bypassed
    # by some process writing directly into control/requests/.
    consume_send_nonce(params.get("send_nonce"), to, text, service)

    # Require explicit human approval before AppleScript sends. The native
    # helper shows both the resolved and raw recipient plus the complete body
    # in a scrollable view. Cancel is the keyboard default and all unexpected
    # outcomes fail closed.
    resolved_name = _resolve_contact_name(to, contacts)
    if not _run_send_confirmation(
        to=to,
        resolved_name=resolved_name,
        service=service,
        text=text,
    ):
        raise RuntimeError(
            "send cancelled by user or timed out (60s dialog limit)"
        )

    if service == "iMessage":
        svc_clause = "1st service whose service type = iMessage"
    else:  # SMS — already validated against _SERVICE_ENUM
        svc_clause = "1st service whose service type = SMS"

    # Write the body to a tempfile, give AppleScript a POSIX path to it.
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".txt", delete=False,
        prefix="cowork_imessage_send_",
    ) as f:
        f.write(text)
        body_path = f.name

    try:
        script = (
            f'set msgBody to read POSIX file "{_escape_as_string(body_path)}" '
            f'as «class utf8»\n'
            f'tell application "Messages"\n'
            f'    set svc to {svc_clause}\n'
            f'    send msgBody to buddy "{_escape_as_string(to)}" of svc\n'
            f'end tell\n'
        )
        rc, stdout, stderr = _run_osascript(script)
        if rc != 0:
            raise RuntimeError(
                f"osascript send failed (rc={rc}): "
                f"{stderr or stdout or 'no output'}"
            )
    finally:
        try:
            os.unlink(body_path)
        except OSError:
            pass

    return {
        "sent": {
            "to": to,
            "resolved_name": _resolve_contact_name(to, contacts),
            "service": service,
            "text_length": len(text),
            "sent_at": datetime.now().isoformat(timespec="seconds"),
        }
    }


action_send.needs_db = False  # type: ignore[attr-defined]


ACTIONS = {
    "status": action_status,
    "review": action_review,
    "search": action_search,
    "chat_history": action_chat_history,
    "response_stats": action_response_stats,
    "contacts_lookup": action_contacts_lookup,
    "list_chats": action_list_chats,
    "send_preview": action_send_preview,
    "send": action_send,
}


# ---------------------------------------------------------------------------
# Request / response plumbing
# ---------------------------------------------------------------------------
def write_response(req_filename_stem: str, data: dict) -> None:
    """Write response JSON atomically. Uses the request filename stem to
    derive the response filename, never trusting JSON id for the path.
    """
    if not req_filename_stem or "/" in req_filename_stem or req_filename_stem in (".", ".."):
        raise ValueError("invalid response filename stem")
    name = f"response-{req_filename_stem}.json"
    tmp = f".{name}.{uuid.uuid4().hex}.tmp"
    with _private_directory_fd(RESPONSES_DIR, create=True) as responses_fd:
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_NOFOLLOW_FLAGS,
            0o600,
            dir_fd=responses_fd,
        )
        try:
            _validate_regular_file(fd, tmp, private=True)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd = -1
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, name, src_dir_fd=responses_fd, dst_dir_fd=responses_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp, dir_fd=responses_fd)
            except FileNotFoundError:
                pass


def reap_expired_responses() -> None:
    """Remove response payloads that the host failed to consume promptly."""
    now = time.time()
    try:
        with _private_directory_fd(RESPONSES_DIR) as responses_fd:
            for name in os.listdir(responses_fd):
                if not (name.startswith("response-") and name.endswith(".json")):
                    continue
                try:
                    # Legacy releases may have left broader file modes. The
                    # containing directory is private; unlinking a verified
                    # regular, current-user-owned file does not follow it.
                    metadata = _stat_regular_at(responses_fd, name)
                    if now - metadata.st_mtime > RESPONSE_TTL_S:
                        os.unlink(name, dir_fd=responses_fd)
                except (OSError, UnsafeRuntimePath) as e:
                    log(f"response reaper could not remove {name}: {e}")
    except UnsafeRuntimePath as e:
        if isinstance(e.__cause__, FileNotFoundError):
            return
        raise


def _read_request_text(req_path: Path, requests_fd: int | None) -> str:
    if requests_fd is None:
        fd = os.open(req_path, os.O_RDONLY | _FILE_NOFOLLOW_FLAGS)
    else:
        fd = os.open(
            req_path.name,
            os.O_RDONLY | _FILE_NOFOLLOW_FLAGS,
            dir_fd=requests_fd,
        )
    try:
        metadata = _validate_regular_file(fd, str(req_path))
        if metadata.st_size > MAX_REQUEST_BYTES:
            raise ValueError(f"request exceeds {MAX_REQUEST_BYTES} byte limit")
        chunks: list[bytes] = []
        remaining = MAX_REQUEST_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError(f"request exceeds {MAX_REQUEST_BYTES} byte limit")
        return raw.decode("utf-8")
    finally:
        os.close(fd)


def _bad_request(req_stem: str, error: str, *, req_id: str | None = None) -> None:
    response: dict[str, Any] = {"ok": False, "error": error,
                                "allowed_actions": sorted(allowed_actions())}
    if req_id is not None:
        response["id"] = req_id
    write_response(req_stem, response)


def process_request(
    req_path: Path,
    privacy_policy: PrivacyPolicy | list[str],
    *,
    requests_fd: int | None = None,
) -> None:
    # Derive safe response filename from request filename stem only.
    # Never use JSON id field for filesystem paths — it could contain slashes.
    req_stem = req_path.stem.replace("request-", "")
    if not req_stem:
        log(f"skipping malformed request filename: {req_path.name}")
        return

    # Ignore incomplete files: *.tmp, *.partial, names starting with .
    if (req_path.suffix in (".tmp", ".partial") or
        req_path.name.startswith(".") or
        req_path.name.startswith("request-") and not req_path.name.endswith(".json")):
        return

    try:
        raw = _read_request_text(req_path, requests_fd)
    except Exception as e:
        _bad_request(req_stem, f"bad request file: {e}")
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Preserve compatibility with non-atomic clients by retrying malformed
        # JSON once. Secure descriptor-relative opens still reject symlinks.
        time.sleep(0.1)
        try:
            raw = _read_request_text(req_path, requests_fd)
            data = json.loads(raw)
        except Exception as e:
            _bad_request(req_stem, f"bad request JSON: {e}")
            return

    if not isinstance(data, dict):
        _bad_request(req_stem, "bad request: JSON root must be an object")
        return

    # Echo back the JSON id in response, but never use it for filesystem paths.
    req_id = str(data.get("id", req_stem))
    action = data.get("action")
    params = data.get("params", {})

    if not isinstance(action, str):
        _bad_request(req_stem, "bad request: action must be a string", req_id=req_id)
        return
    if not isinstance(params, dict):
        _bad_request(req_stem, "bad request: params must be an object", req_id=req_id)
        return

    permitted = allowed_actions()
    if action not in ACTIONS:
        write_response(req_stem, {
            "id": req_id,
            "ok": False,
            "error": f"unknown action: {action!r}",
            "allowed_actions": sorted(permitted),
        })
        return
    if action not in permitted:
        # Role gate: enforced here in the worker, never only in a host or
        # app layer. A manager bridge never serves bodies; a host bridge
        # never enumerates chats.
        write_response(req_stem, {
            "id": req_id,
            "ok": False,
            "error": "action not permitted on this bridge",
            "bridge_role": bridge_role(),
            "allowed_actions": sorted(permitted),
        })
        return

    db_path = None
    try:
        action_fn = ACTIONS[action]
        # Send-side actions declare needs_db=False; skip the (potentially
        # hundreds-of-MB) chat.db snapshot on that path.
        needs_db = getattr(action_fn, "needs_db", True)
        conn = None
        if needs_db:
            db_path = copy_chatdb()
            conn = open_snapshot(db_path)
        needs_contacts = getattr(action_fn, "needs_contacts", True)
        contacts = load_contacts() if needs_contacts else {}
        result = action_fn(params, conn, contacts, privacy_policy)
        if conn is not None:
            conn.close()
        result.update({"id": req_id, "action": action, "ok": True,
                       "generated_at": datetime.now().isoformat(timespec="seconds")})
        write_response(req_stem, result)
    except Exception as e:
        log(f"action={action} id={req_id} error: {e!r}")
        log(traceback.format_exc())
        write_response(req_stem, {
            "id": req_id, "action": action, "ok": False, "error": str(e),
            "allowed_actions": sorted(permitted),
        })
    finally:
        if db_path is not None:
            cleanup_tmpdb(db_path)


def _acquire_bridge_lock(control_fd: int, timeout_s: float = OSASCRIPT_TIMEOUT_S + 10.0) -> int:
    """Acquire an exclusive lock on control/lock with bounded wait.

    Returns the lock file descriptor on success. The caller must close it
    to release the lock. Raises RuntimeError on timeout or failure.
    """
    open_deadline = time.time() + timeout_s
    while True:
        try:
            lock_fd = os.open(
                "lock",
                os.O_CREAT | os.O_RDWR | _FILE_NOFOLLOW_FLAGS,
                0o600,
                dir_fd=control_fd,
            )
            break
        except FileNotFoundError:
            if time.time() >= open_deadline:
                message = "could not create bridge lock file"
                log(message)
                raise RuntimeError(message)
            time.sleep(0.01)
        except OSError as exc:
            message = f"could not open bridge lock file: {exc}"
            log(message)
            raise RuntimeError(message) from exc
    try:
        _validate_regular_file(lock_fd, "control/lock", private=True)
    except Exception as exc:
        os.close(lock_fd)
        message = f"unsafe bridge lock file: {exc}"
        log(message)
        raise RuntimeError(message) from exc

    # Try non-blocking lock first
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fd
    except (OSError, BlockingIOError):
        pass

    # Lock is held; poll with bounded wait
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(0.05)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_fd
        except (OSError, BlockingIOError):
            continue

    message = (
        f"could not acquire bridge lock within {timeout_s}s timeout; "
        "another worker is processing this bridge"
    )
    log(message)
    os.close(lock_fd)
    raise RuntimeError(message)


def main() -> None:
    for path in (LOG_PATH.parent, REQUESTS_DIR, RESPONSES_DIR):
        with _private_directory_fd(path, create=True):
            pass
    privacy_policy = load_privacy_policy()

    reap_expired_responses()

    # v0.4.0+: garbage-collect stale send nonces from previews that never
    # got a matching send (user cancelled, the host stopped before sending). Cheap; touches
    # only ~/imessage-bridge/nonces/ and only a few files at most.
    # Manager role: skip nonce reaping (no nonces directory created).
    if bridge_role() != "manager":
        try:
            reap_expired_nonces()
        except Exception as e:
            log(f"reap_expired_nonces error: {e!r}")

    # Acquire per-bridge advisory lock to serialize workers on this bridge.
    # Hold for the entire drain; re-list requests once after acquiring to
    # catch any that arrived during the lock wait. The lock is released
    # automatically when lock_fd is closed on exit.
    try:
        with _private_directory_fd(LOG_PATH.parent) as control_fd:
            lock_fd = _acquire_bridge_lock(control_fd)
    except RuntimeError as e:
        log(f"bridge lock unavailable, deferring drain: {e}")
        return

    try:
        # Only process complete request files (*.json, not temp/partial suffixes).
        with _private_directory_fd(REQUESTS_DIR) as requests_fd:
            pending = sorted(
                name
                for name in os.listdir(requests_fd)
                if name.startswith("request-") and name.endswith(".json")
            )
            if not pending:
                # launchd sometimes fires with no new file (e.g. directory-touch).
                return

            for name in pending:
                request = Path(name)
                req_stem = request.stem.replace("request-", "")
                try:
                    process_request(request, privacy_policy, requests_fd=requests_fd)
                except Exception as e:
                    log(f"request={name} unhandled error: {e!r}")
                    try:
                        _bad_request(req_stem, f"request processing failed: {e}")
                    except Exception as response_error:
                        log(f"request={name} could not write error response: {response_error!r}")
                finally:
                    try:
                        os.unlink(name, dir_fd=requests_fd)
                    except Exception as e:
                        log(f"could not unlink {name}: {e}")
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    main()
