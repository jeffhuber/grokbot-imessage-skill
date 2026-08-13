"""Helper-side send confirmation gate for imessage-review.

send_preview mints a single-use nonce tied to the previewed payload.
send requires the nonce back, and the payload must match what was
previewed exactly. Nonces expire after SEND_NONCE_TTL seconds and are
deleted on use so they can't be replayed.

This enforces "preview-then-confirm" on the helper, so a process that
has managed to forge an authenticated `send` request still can't send
a message without first going through a `send_preview` that the user
sees in the AI host's UI.

Design note: the helper is spawned per request by launchd (WatchPaths),
so it exits between `send_preview` and `send`. Nonces are therefore
persisted as per-nonce files under ``<bridge>/nonces/`` rather than
kept in memory.
"""
import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import stat
import time
from contextlib import contextmanager


SEND_NONCE_TTL = 60  # seconds

# URL-safe base64 alphabet used by secrets.token_urlsafe
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DIR_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_NOFOLLOW_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_MAX_NONCE_RECORD_BYTES = 4096


class SendGateError(Exception):
    """Raised when a send request fails to clear the preview/confirm gate."""


def _bridge_dir() -> pathlib.Path:
    override = os.environ.get("IMESSAGE_BRIDGE_DIR") or os.environ.get("COWORK_IMESSAGE_BRIDGE_DIR")
    if not override:
        raise RuntimeError(
            "IMESSAGE_BRIDGE_DIR is required "
            "(COWORK_IMESSAGE_BRIDGE_DIR remains a one-release compatibility alias)"
        )
    return pathlib.Path(os.path.abspath(os.path.expanduser(override)))


def _validate_private_directory(fd: int, label: str) -> None:
    metadata = os.fstat(fd)
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"{label} is not a directory")
    if metadata.st_uid != os.getuid():
        raise RuntimeError(f"{label} is not owned by the current user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError(f"{label} must not have group/world permissions")


def _open_bridge_root() -> int:
    root = _bridge_dir()
    if not root.is_absolute() or root == pathlib.Path("/"):
        raise RuntimeError("bridge root must be a non-root absolute path")
    fd = os.open("/", _DIR_OPEN_FLAGS)
    try:
        for component in root.parts[1:]:
            next_fd = os.open(component, _DIR_OPEN_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        _validate_private_directory(fd, f"bridge root {root}")
        return fd
    except RuntimeError:
        os.close(fd)
        raise
    except OSError as exc:
        os.close(fd)
        raise RuntimeError(f"unsafe bridge root {root}: {exc}") from exc


@contextmanager
def _nonce_dir_fd():
    fd = _open_bridge_root()
    try:
        try:
            os.mkdir("nonces", mode=0o700, dir_fd=fd)
        except FileExistsError:
            pass
        try:
            nonce_fd = os.open("nonces", _DIR_OPEN_FLAGS, dir_fd=fd)
        except OSError as exc:
            raise RuntimeError(f"unsafe nonce directory: {exc}") from exc
        os.close(fd)
        fd = nonce_fd
        _validate_private_directory(fd, "nonce directory")
        yield fd
    finally:
        os.close(fd)


def _validate_nonce_file(fd: int, label: str) -> None:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"{label} is not a regular file")
    if metadata.st_uid != os.getuid():
        raise RuntimeError(f"{label} is not owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RuntimeError(f"{label} must have mode 600")
    if metadata.st_size > _MAX_NONCE_RECORD_BYTES:
        raise RuntimeError(f"{label} is too large")


def _read_nonce_record(directory_fd: int, name: str) -> dict:
    fd = os.open(name, os.O_RDONLY | _FILE_NOFOLLOW_FLAGS, dir_fd=directory_fd)
    try:
        _validate_nonce_file(fd, name)
        chunks = []
        remaining = _MAX_NONCE_RECORD_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _MAX_NONCE_RECORD_BYTES:
            raise RuntimeError(f"{name} is too large")
        record = json.loads(raw.decode("utf-8"))
        if not isinstance(record, dict):
            raise RuntimeError(f"{name} must contain a JSON object")
        return record
    finally:
        os.close(fd)


def _unlink_at(directory_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def _valid_expiry(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


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
    name = f"{nonce}.json"
    tmp = f".{nonce}.json.tmp"
    with _nonce_dir_fd() as nonce_fd:
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_NOFOLLOW_FLAGS,
            0o600,
            dir_fd=nonce_fd,
        )
        try:
            _validate_nonce_file(fd, tmp)
            with os.fdopen(fd, "w") as f:
                fd = -1
                json.dump(record, f)
            os.replace(tmp, name, src_dir_fd=nonce_fd, dst_dir_fd=nonce_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            _unlink_at(nonce_fd, tmp)
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

    name = f"{nonce}.json"
    claimed = f"{nonce}.claimed"
    with _nonce_dir_fd() as nonce_fd:
        # Atomically claim the nonce file to prevent concurrent consumption.
        try:
            os.rename(name, claimed, src_dir_fd=nonce_fd, dst_dir_fd=nonce_fd)
        except FileNotFoundError:
            raise SendGateError(
                "nonce not recognized; send_preview must precede send"
            )
        except Exception as e:
            raise SendGateError(f"failed to claim nonce: {e}")

        # Now validate the claimed nonce (only one process got here).
        try:
            record = _read_nonce_record(nonce_fd, claimed)
        except Exception as e:
            _unlink_at(nonce_fd, claimed)
            raise SendGateError(f"malformed nonce record: {e}")

        expires_at = record.get("expires_at", 0)
        if not _valid_expiry(expires_at):
            _unlink_at(nonce_fd, claimed)
            raise SendGateError("malformed nonce record: expires_at must be a finite number")
        if int(time.time()) > expires_at:
            _unlink_at(nonce_fd, claimed)
            raise SendGateError(
                f"nonce expired (TTL {SEND_NONCE_TTL}s); call send_preview again"
            )

        if _preview_hash(to, body, service) != record.get("preview_hash"):
            _unlink_at(nonce_fd, claimed)
            raise SendGateError(
                "send payload differs from preview; re-preview required"
            )

        # One-shot: delete on success so the nonce can't be replayed.
        _unlink_at(nonce_fd, claimed)


def reap_expired_nonces() -> None:
    """Garbage-collect stale nonce files from previews that never got a
    matching send (user cancelled, the host stopped before sending, etc.). Safe to call
    at helper startup. Only reaps files older than TTL or malformed files
    with mtime older than a short grace period to avoid deleting fresh
    mid-write records. Also cleans up orphaned .claimed files."""
    now = int(time.time())
    grace_period = 5  # seconds; avoid reaping files being written right now
    with _nonce_dir_fd() as nonce_fd:
        for name in os.listdir(nonce_fd):
            if name.startswith("."):
                continue
            if not (name.endswith(".json") or name.endswith(".claimed")):
                continue
            try:
                metadata = os.stat(name, dir_fd=nonce_fd, follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                    continue
                if stat.S_IMODE(metadata.st_mode) & 0o077:
                    continue
                age = now - metadata.st_mtime
                if name.endswith(".claimed"):
                    if age > SEND_NONCE_TTL:
                        _unlink_at(nonce_fd, name)
                    continue
                if age < grace_period:
                    continue
                try:
                    record = _read_nonce_record(nonce_fd, name)
                    expires_at = record.get("expires_at", 0)
                    if not _valid_expiry(expires_at) or now > expires_at:
                        _unlink_at(nonce_fd, name)
                except Exception:
                    if age > grace_period:
                        _unlink_at(nonce_fd, name)
            except Exception:
                pass
