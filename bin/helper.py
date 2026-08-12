#!/usr/bin/env python3
"""cowork-imessage helper

Runs on macOS, triggered by launchd when a new file lands in control/requests/.
Scans the request queue, dispatches each whitelisted action against a snapshot
of ~/Library/Messages/chat.db, writes a response JSON into control/responses/,
and deletes the request.

Security posture:
  - Actions are strictly whitelisted (no eval/exec/shell-out).
  - All SQL uses parameterized queries.
  - chat.db is copied to a per-run tempfile (cleaned up on exit).
  - Blocklisted chats are dropped before any message text is returned.
  - 2FA codes, card numbers, and SSN patterns are redacted in responses.
  - Response writes are atomic (tmp + rename) so the agent never reads a
    half-written file.

This script should be invoked only by the signed `cowork-imessage-helper`
wrapper. Running it directly still works but without the environment
hardening the wrapper provides.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sqlite3
import struct
import sys
import tempfile
import time
import traceback
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
INSTALL_ROOT = Path(__file__).resolve().parent.parent
REQUESTS_DIR = INSTALL_ROOT / "control" / "requests"
RESPONSES_DIR = INSTALL_ROOT / "control" / "responses"
LOG_PATH = INSTALL_ROOT / "control" / "log.txt"
BLOCKLIST_PATH = INSTALL_ROOT / "contacts" / "blocked_chats.txt"

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
    path = INSTALL_ROOT / "bin" / f"{name}.py"
    spec = _importlib_util.spec_from_file_location(name, path)
    mod = _importlib_util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Route send_gate's state to our install tree so a non-default install
# (helper lives somewhere other than ~/cowork-imessage/) still works.
os.environ.setdefault("COWORK_IMESSAGE_BRIDGE_DIR", str(INSTALL_ROOT))
_send_gate = _load_sibling("send_gate")
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
MAX_TEXT_SNIPPET = 600
MAX_CONTEXT_MESSAGES = 8

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
# Logging
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# attributedBody typedstream decoder (pure Python, no PyObjC)
# Ported from the original Perplexity skill and kept byte-compatible.
# ---------------------------------------------------------------------------
def decode_attributed_body(blob: bytes | None) -> str:
    if not blob:
        return ""
    try:
        data = bytes(blob)
    except Exception:
        return ""
    if b"streamtyped" not in data[:16]:
        return ""

    idx = data.find(b"NSString")
    if idx == -1:
        return ""
    p = idx + len(b"NSString") + 1

    while p < len(data) and data[p] in (0x84, 0x94, 0x85, 0x95, 0x01, 0x86):
        p += 1

    if p + 8 < len(data) and data[p : p + 8] == b"NSObject":
        p += 8
        while p < len(data) and data[p] in (0x84, 0x94, 0x85, 0x95, 0x01, 0x86):
            p += 1

    if p < len(data) and data[p] == 0x2B:
        p += 1

    if p >= len(data):
        return ""

    b0 = data[p]
    if b0 == 0x81:
        length = struct.unpack("<H", data[p + 1 : p + 3])[0]
        p += 3
    elif b0 == 0x82:
        length = struct.unpack("<I", data[p + 1 : p + 5])[0]
        p += 5
    elif b0 < 0x80:
        length = b0
        p += 1
    else:
        p += 1
        if p >= len(data):
            return ""
        b0 = data[p]
        if b0 == 0x81:
            length = struct.unpack("<H", data[p + 1 : p + 3])[0]
            p += 3
        elif b0 < 0x80:
            length = b0
            p += 1
        else:
            return ""

    if length <= 0 or p + length > len(data):
        return ""

    try:
        return data[p : p + length].decode("utf-8", errors="replace")
    except Exception:
        return ""


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


def load_chat_participants(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Return {chat_identifier: [participant_handle, ...]} for every chat.

    Used to build a human label for group chats whose chat_identifier is
    just "chatNNNNN…" and whose display_name is empty. With participants
    in hand we can render e.g. "Alice, Bob & 2 others" instead of the
    opaque group id.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.chat_identifier, h.id
        FROM chat c
        JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
        JOIN handle h ON h.ROWID = chj.handle_id
        """
    )
    out: dict[str, list[str]] = defaultdict(list)
    for chat_ident, handle_id in cur.fetchall():
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
# Blocklist
# ---------------------------------------------------------------------------
def load_blocklist() -> list[str]:
    if not BLOCKLIST_PATH.exists():
        return []
    out = []
    for line in BLOCKLIST_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _last10(s: str) -> str:
    d = re.sub(r"[^0-9]", "", s or "")
    return d[-10:] if len(d) >= 10 else ""


def is_blocked(chat_id: str, sender: str, blocklist: list[str]) -> bool:
    cid = chat_id or ""
    snd = sender or ""
    cid_l10 = _last10(cid)
    snd_l10 = _last10(snd)
    for b in blocklist:
        b_l10 = _last10(b)
        if b_l10 and (b_l10 == cid_l10 or b_l10 == snd_l10):
            return True
        # Non-numeric blocklist entries match as case-insensitive substring
        # against chat_id / sender (covers email handles and group IDs).
        if not b_l10:
            bl = b.lower()
            if bl in cid.lower() or bl in snd.lower():
                return True
    return False


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


def validate_chat(v: Any) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ValueError("chat identifier required")
    if len(v) > 200:
        raise ValueError("chat identifier too long")
    return v.strip()


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


# ---------------------------------------------------------------------------
# DB handling
# ---------------------------------------------------------------------------
def copy_chatdb() -> Path:
    src = Path.home() / "Library" / "Messages" / "chat.db"
    if not src.exists():
        raise RuntimeError(f"chat.db not found at {src}")
    fd, tmp = tempfile.mkstemp(prefix="cowork_imessage_", suffix=".db")
    os.close(fd)
    shutil.copy2(src, tmp)
    # WAL sidecars — copy if present so the snapshot is consistent.
    for sidecar in ("-wal", "-shm"):
        sc = Path(str(src) + sidecar)
        if sc.exists():
            try:
                shutil.copy2(sc, tmp + sidecar)
            except Exception as e:
                log(f"sidecar {sidecar}: {e}")
    return Path(tmp)


def cleanup_tmpdb(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass


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


def apply_blocklist(msgs: list[dict], blocklist: list[str]) -> list[dict]:
    return [m for m in msgs if not is_blocked(m["chat_id"], m["sender"], blocklist)]


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
def action_review(params, conn, contacts, blocklist):
    days = validate_days(params.get("days", 2))
    cutoff_ns = to_apple_ns(time.time() - days * 86400)
    msgs = fetch_messages(conn, cutoff_ns)
    msgs = apply_blocklist(msgs, blocklist)
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


def action_search(params, conn, contacts, blocklist):
    term = validate_search(params.get("term"))
    days = validate_days(params.get("days", 30))
    limit = validate_limit(params.get("limit", 100))
    cutoff_ns = to_apple_ns(time.time() - days * 86400)
    msgs = fetch_messages(conn, cutoff_ns, search=term)
    msgs = apply_blocklist(msgs, blocklist)
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


def action_chat_history(params, conn, contacts, blocklist):
    chat_q = validate_chat(params.get("chat"))
    days = validate_days(params.get("days", 14))
    limit = validate_limit(params.get("limit", 100))
    substr = resolve_chat_filter(chat_q, contacts)
    cutoff_ns = to_apple_ns(time.time() - days * 86400)
    msgs = fetch_messages(conn, cutoff_ns, chat_filter_substr=substr)
    msgs = apply_blocklist(msgs, blocklist)
    msgs.sort(key=lambda x: x["ts_ns"])
    msgs = msgs[-limit:]
    out = []
    for m in msgs:
        name = lookup_name(m["chat_id"], m["sender"], contacts)
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


def action_response_stats(params, conn, contacts, blocklist):
    chat_q = validate_chat(params.get("chat"))
    hours = validate_hours(params.get("hours", 24))
    substr = resolve_chat_filter(chat_q, contacts)
    cutoff_ns = to_apple_ns(time.time() - hours * 3600)
    msgs = fetch_messages(conn, cutoff_ns, chat_filter_substr=substr)
    msgs = apply_blocklist(msgs, blocklist)
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


def action_contacts_lookup(params, conn, contacts, blocklist):
    name = params.get("name", "")
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValueError("name must be a 1..100 char string")
    nl = name.lower()
    matches = []
    for digits, full_name in contacts.items():
        if nl in full_name.lower():
            matches.append({"name": full_name, "phone_last10": digits})
    return {"query": name, "match_count": len(matches), "matches": matches[:25]}


# ---------------------------------------------------------------------------
# Send actions (AppleScript-driven)
# ---------------------------------------------------------------------------
def _resolve_contact_name(to: str, contacts: dict[str, str]) -> str:
    """Best-effort name lookup from phone-shaped targets.

    Emails and group-chat IDs return "" — not because we couldn't look
    them up, but because the Contacts-side loader keys on normalized
    10-digit phone numbers only.
    """
    key = _last10(to)
    return contacts.get(key, "") if key else ""


def action_send_preview(params, conn, contacts, blocklist):
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
    to = validate_chat(params.get("to"))
    text = validate_send_text(params.get("text"))
    service = validate_service(params.get("service"))

    send_nonce = mint_send_nonce(to, text, service)

    return {
        "preview": {
            "to": to,
            "resolved_name": _resolve_contact_name(to, contacts),
            "service": service,
            "text": text,
            "text_length": len(text),
            "blocked": is_blocked(to, to, blocklist),
        },
        "send_nonce": send_nonce,
        "send_nonce_ttl_seconds": SEND_NONCE_TTL,
    }


action_send_preview.needs_db = False  # type: ignore[attr-defined]


def action_send(params, conn, contacts, blocklist):
    """Send an iMessage (or SMS via iPhone relay) via AppleScript.

    The message body is written to a tempfile and read by AppleScript as
    UTF-8, which sidesteps every AppleScript string-escape bug and lets us
    send arbitrary Unicode (including emoji and newlines) unchanged.

    Recipient identifiers are escaped inline as AppleScript string literals
    because they've already passed `validate_chat` (≤200 chars, stripped).

    The `service type` slot is an AppleScript enum, not a string. We pick
    the clause statically from the validated service name so no untrusted
    input is ever interpolated into that slot.
    """
    to = validate_chat(params.get("to"))
    text = validate_send_text(params.get("text"))
    service = validate_service(params.get("service"))

    if is_blocked(to, to, blocklist):
        raise ValueError(
            f"refusing to send: {to!r} is in contacts/blocked_chats.txt"
        )

    # v0.4.0+: helper-side send gate. `send_preview` must have been called
    # first for this exact (to, text, service) triple, and the resulting
    # single-use nonce must be echoed back within the TTL window. This
    # enforces preview-then-confirm even if the bridge has been bypassed
    # by some process writing directly into control/requests/.
    consume_send_nonce(params.get("send_nonce"), to, text, service)

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
    "review": action_review,
    "search": action_search,
    "chat_history": action_chat_history,
    "response_stats": action_response_stats,
    "contacts_lookup": action_contacts_lookup,
    "send_preview": action_send_preview,
    "send": action_send,
}


# ---------------------------------------------------------------------------
# Request / response plumbing
# ---------------------------------------------------------------------------
def write_response(req_id: str, data: dict) -> None:
    RESPONSES_DIR.mkdir(parents=True, exist_ok=True)
    path = RESPONSES_DIR / f"response-{req_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def process_request(req_path: Path, blocklist: list[str]) -> None:
    try:
        raw = req_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as e:
        req_id = req_path.stem.replace("request-", "") or str(uuid.uuid4())
        write_response(req_id, {"ok": False, "error": f"bad request JSON: {e}"})
        return

    req_id = str(data.get("id") or req_path.stem.replace("request-", "") or uuid.uuid4())
    action = data.get("action")
    params = data.get("params", {}) or {}

    if action not in ACTIONS:
        write_response(req_id, {
            "id": req_id,
            "ok": False,
            "error": f"unknown action: {action!r}",
            "allowed_actions": sorted(ACTIONS.keys()),
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
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.text_factory = bytes
        contacts = load_contacts()
        result = action_fn(params, conn, contacts, blocklist)
        if conn is not None:
            conn.close()
        result.update({"id": req_id, "action": action, "ok": True,
                       "generated_at": datetime.now().isoformat(timespec="seconds")})
        write_response(req_id, result)
    except Exception as e:
        log(f"action={action} id={req_id} error: {e!r}")
        log(traceback.format_exc())
        write_response(req_id, {
            "id": req_id, "action": action, "ok": False, "error": str(e),
        })
    finally:
        if db_path is not None:
            cleanup_tmpdb(db_path)


def main() -> None:
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    RESPONSES_DIR.mkdir(parents=True, exist_ok=True)
    blocklist = load_blocklist()

    # v0.4.0+: garbage-collect stale send nonces from previews that never
    # got a matching send (user cancelled, Claude crashed). Cheap; touches
    # only ~/cowork-imessage/nonces/ and only a few files at most.
    try:
        reap_expired_nonces()
    except Exception as e:
        log(f"reap_expired_nonces error: {e!r}")

    pending = sorted(REQUESTS_DIR.glob("request-*.json"))
    if not pending:
        # launchd sometimes fires with no new file (e.g. directory-touch).
        return

    for p in pending:
        try:
            process_request(p, blocklist)
        finally:
            try:
                p.unlink()
            except Exception as e:
                log(f"could not unlink {p.name}: {e}")


if __name__ == "__main__":
    main()
