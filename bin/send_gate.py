"""Helper-side send confirmation gate for imessage-review.

send_preview mints a single-use nonce tied to the previewed payload.
send requires the nonce back, and the payload must match what was
previewed exactly. Nonces expire after SEND_NONCE_TTL seconds and are
deleted on use so they can't be replayed.

This enforces "preview-then-confirm" on the helper, so a process that
has managed to forge an authenticated `send` request still can't send
a message without first going through a `send_preview` that the user
sees in Claude.

Design note: the helper is spawned per request by launchd (WatchPaths),
so it exits between `send_preview` and `send`. Nonces are therefore
persisted as per-nonce files under ``<bridge>/nonces/`` rather than
kept in memory.
"""
import hashlib
import json
import os
import pathlib
import re
import secrets
import time


SEND_NONCE_TTL = 60  # seconds

# URL-safe base64 alphabet used by secrets.token_urlsafe
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class SendGateError(Exception):
    """Raised when a send request fails to clear the preview/confirm gate."""


def _bridge_dir() -> pathlib.Path:
    override = os.environ.get("COWORK_IMESSAGE_BRIDGE_DIR")
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / "cowork-imessage"


def _nonce_dir() -> pathlib.Path:
    d = _bridge_dir() / "nonces"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _preview_hash(to: str, body: str, service: str) -> str:
    """Bind a nonce to its exact payload. Null bytes separate fields
    so ``('ab', 'c')`` and ``('a', 'bc')`` don't collide."""
    h = hashlib.sha256()
    for part in (to, body, service):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def mint_send_nonce(to: str, body: str, service: str) -> str:
    """Called from action_send_preview. Persists a nonce record and
    returns the nonce string the client must echo back on send.
    Atomic write via temp file + rename to prevent reap from deleting
    mid-write records."""
    nonce = secrets.token_urlsafe(24)  # ~32 chars, URL/filename safe
    record = {
        "preview_hash": _preview_hash(to, body, service),
        "expires_at": int(time.time()) + SEND_NONCE_TTL,
    }
    nonce_dir = _nonce_dir()
    path = nonce_dir / f"{nonce}.json"
    tmp = nonce_dir / f".{nonce}.json.tmp"
    # Write to temp file first, then rename atomically into place.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(record, f)
    os.rename(tmp, path)
    return nonce


def consume_send_nonce(nonce, to: str, body: str, service: str) -> None:
    """Called from action_send. Raises SendGateError on any failure;
    deletes the nonce on success so it can't be reused.

    Atomically claims the nonce file to prevent concurrent double-send races."""
    if not isinstance(nonce, str) or not nonce:
        raise SendGateError("missing nonce; call send_preview first")
    if not _NONCE_RE.match(nonce):
        # Defense-in-depth against path-traversal via a crafted nonce.
        raise SendGateError("invalid nonce format")

    path = _nonce_dir() / f"{nonce}.json"
    claimed_path = _nonce_dir() / f"{nonce}.claimed"

    # Atomically claim the nonce file to prevent concurrent consumption.
    try:
        path.rename(claimed_path)
    except FileNotFoundError:
        raise SendGateError(
            "nonce not recognized; send_preview must precede send"
        )
    except Exception as e:
        raise SendGateError(f"failed to claim nonce: {e}")

    # Now validate the claimed nonce (only one process got here).
    try:
        record = json.loads(claimed_path.read_text())
    except Exception as e:
        claimed_path.unlink(missing_ok=True)
        raise SendGateError(f"malformed nonce record: {e}")

    if int(time.time()) > record.get("expires_at", 0):
        claimed_path.unlink(missing_ok=True)
        raise SendGateError(
            f"nonce expired (TTL {SEND_NONCE_TTL}s); call send_preview again"
        )

    if _preview_hash(to, body, service) != record.get("preview_hash"):
        claimed_path.unlink(missing_ok=True)
        raise SendGateError(
            "send payload differs from preview; re-preview required"
        )

    # One-shot: delete on success so the nonce can't be replayed.
    claimed_path.unlink(missing_ok=True)


def reap_expired_nonces() -> None:
    """Garbage-collect stale nonce files from previews that never got a
    matching send (user cancelled, Claude crashed, etc.). Safe to call
    at helper startup. Only reaps files older than TTL or malformed files
    with mtime older than a short grace period to avoid deleting fresh
    mid-write records. Also cleans up orphaned .claimed files."""
    now = int(time.time())
    grace_period = 5  # seconds; avoid reaping files being written right now
    nonce_dir = _nonce_dir()

    # Reap normal .json nonce files.
    for p in nonce_dir.glob("*.json"):
        # Skip temp files and claimed files (they're transient).
        if p.name.startswith(".") or p.suffix == ".tmp" or ".claimed" in p.name:
            continue
        try:
            # Check file mtime; if very fresh, skip it (may be mid-write).
            stat = p.stat()
            if now - stat.st_mtime < grace_period:
                continue
            record = json.loads(p.read_text())
            if now > record.get("expires_at", 0):
                p.unlink(missing_ok=True)
        except Exception:
            # Malformed or racing; only delete if not brand-new.
            try:
                stat = p.stat()
                if now - stat.st_mtime > grace_period:
                    p.unlink(missing_ok=True)
            except Exception:
                pass

    # Reap orphaned .claimed files (helper died after claiming but before sending).
    for p in nonce_dir.glob("*.claimed"):
        try:
            stat = p.stat()
            # Delete claimed files older than TTL (they're abandoned).
            if now - stat.st_mtime > SEND_NONCE_TTL:
                p.unlink(missing_ok=True)
        except Exception:
            pass
