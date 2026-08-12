from __future__ import annotations

import json
import os
import stat
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests._helper_loader import helper


def make_attributed_blob(body: bytes) -> bytes:
    header = b"streamtyped\x00\x00\x00\x00\x00"
    prefix = header + b"NSString\x01\x2b"
    if len(body) < 0x80:
        return prefix + bytes([len(body)]) + body
    if len(body) < 0x10000:
        return prefix + b"\x81" + struct.pack("<H", len(body)) + body
    return prefix + b"\x82" + struct.pack("<I", len(body)) + body


class ValidationTests(unittest.TestCase):
    def test_send_recipient_accepts_supported_handles(self) -> None:
        for value in (
            "+14155551234",
            "(415) 555-1234",
            "415-555-1234",
            "alex@example.com",
        ):
            with self.subTest(value=value):
                self.assertTrue(helper.validate_send_recipient(value))

    def test_send_recipient_rejects_names_groups_and_control_whitespace(self) -> None:
        for value in (
            "chat123",
            "chatABC",
            "Alice Smith",
            "bad@no-dot",
            'foo"bar@example.com',
            ".alice@example.com",
            "alice..example@example.com",
            "alice@-example.com",
            "41555\n51234",
            "41555\r51234",
            "41555\t51234",
            "41555\v51234",
            "41555\f51234",
            "41555\u00a051234",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                helper.validate_send_recipient(value)

    def test_send_text_bounds_and_controls(self) -> None:
        self.assertEqual(helper.validate_send_text("hello\nworld"), "hello\nworld")
        with self.assertRaises(ValueError):
            helper.validate_send_text("")
        with self.assertRaises(ValueError):
            helper.validate_send_text("x" * (helper.MAX_SEND_LEN + 1))
        with self.assertRaises(ValueError):
            helper.validate_send_text("hello\x00world")


class AttributedBodyTests(unittest.TestCase):
    def test_short_medium_and_unicode_bodies(self) -> None:
        for text in ("hello", "x" * 300, "x" * 0x10000, "cafe \U0001f389"):
            with self.subTest(length=len(text)):
                self.assertEqual(
                    helper.decode_attributed_body(make_attributed_blob(text.encode())),
                    text,
                )

    def test_malformed_or_truncated_bodies_fail_closed(self) -> None:
        values = (
            None,
            b"",
            b"not-streamtyped",
            b"streamtyped\x00\x00\x00\x00\x00NSString\x01\x2b\x81",
            b"streamtyped\x00\x00\x00\x00\x00NSString\x01\x2b\x32short",
        )
        sentinel = "private-malformed-body-sentinel"
        values += (
            make_attributed_blob(sentinel.encode())[:-1],
            make_attributed_blob(b"\xff"),
        )
        with mock.patch.object(helper, "log") as log:
            for value in values:
                with self.subTest(value=value):
                    self.assertEqual(helper.decode_attributed_body(value), "")
        logged = " ".join(str(call.args[0]) for call in log.call_args_list)
        self.assertNotIn(sentinel, logged)


class RedactionTests(unittest.TestCase):
    def test_common_secrets_are_redacted(self) -> None:
        test_code = "".join(("937", "461"))
        test_card = "-".join(("4111", "1111", "1111", "1111"))
        test_ssn = "-".join(("321", "54", "9876"))
        cases = (
            (f"Your verification code is {test_code}", test_code, "[REDACTED-2FA]"),
            (f"Card {test_card}", test_card, "[REDACTED-CARD]"),
            (f"SSN {test_ssn}", test_ssn, "[REDACTED-SSN]"),
        )
        for text, secret, marker in cases:
            with self.subTest(marker=marker):
                redacted = helper.redact(text)
                self.assertIn(marker, redacted)
                self.assertNotIn(secret, redacted)

    def test_plain_text_is_unchanged(self) -> None:
        text = "Meet me at 6:30 by the library"
        self.assertEqual(helper.redact(text), text)


class SendGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="grokbot-nonce-test-")
        self.addCleanup(self._tmp.cleanup)
        self._old_bridge = os.environ.get("COWORK_IMESSAGE_BRIDGE_DIR")
        os.environ["COWORK_IMESSAGE_BRIDGE_DIR"] = os.path.realpath(self._tmp.name)
        self.addCleanup(self._restore_bridge)

    def _restore_bridge(self) -> None:
        if self._old_bridge is None:
            os.environ.pop("COWORK_IMESSAGE_BRIDGE_DIR", None)
        else:
            os.environ["COWORK_IMESSAGE_BRIDGE_DIR"] = self._old_bridge

    def test_nonce_round_trip_and_replay_rejection(self) -> None:
        nonce = helper.mint_send_nonce("+14155551234", "hello", "iMessage")
        nonce_path = (
            Path(os.environ["COWORK_IMESSAGE_BRIDGE_DIR"]) / "nonces" / f"{nonce}.json"
        )
        self.assertEqual(stat.S_IMODE(nonce_path.stat().st_mode), 0o600)
        helper.consume_send_nonce(nonce, "+14155551234", "hello", "iMessage")
        with self.assertRaises(helper.SendGateError):
            helper.consume_send_nonce(nonce, "+14155551234", "hello", "iMessage")

    def test_payload_mismatch_burns_nonce(self) -> None:
        nonce = helper.mint_send_nonce("+14155551234", "hello", "iMessage")
        with self.assertRaisesRegex(helper.SendGateError, "differs"):
            helper.consume_send_nonce(nonce, "+14155551234", "changed", "iMessage")
        with self.assertRaises(helper.SendGateError):
            helper.consume_send_nonce(nonce, "+14155551234", "hello", "iMessage")

    def test_malformed_expiry_burns_nonce(self) -> None:
        nonce_dir = Path(os.environ["COWORK_IMESSAGE_BRIDGE_DIR"]) / "nonces"
        for expires_at in ("later", float("nan"), True):
            with self.subTest(expires_at=expires_at):
                nonce = helper.mint_send_nonce("+14155551234", "hello", "iMessage")
                path = nonce_dir / f"{nonce}.json"
                record = json.loads(path.read_text())
                record["expires_at"] = expires_at
                path.write_text(json.dumps(record))
                path.chmod(0o600)

                with self.assertRaisesRegex(helper.SendGateError, "malformed nonce record"):
                    helper.consume_send_nonce(nonce, "+14155551234", "hello", "iMessage")

                self.assertFalse(path.exists())
                self.assertFalse((nonce_dir / f"{nonce}.claimed").exists())

    def test_reaper_preserves_fresh_malformed_and_removes_stale_files(self) -> None:
        nonce_dir = Path(self._tmp.name) / "nonces"
        nonce_dir.mkdir(mode=0o700)
        fresh = nonce_dir / "fresh.json"
        stale = nonce_dir / "stale.json"
        claimed = nonce_dir / "stale.claimed"
        for path in (fresh, stale, claimed):
            path.write_text("{")
            path.chmod(0o600)
        old = time.time() - helper.SEND_NONCE_TTL - 10
        os.utime(stale, (old, old))
        os.utime(claimed, (old, old))

        helper.reap_expired_nonces()

        self.assertTrue(fresh.exists())
        self.assertFalse(stale.exists())
        self.assertFalse(claimed.exists())

    def test_nonce_store_rejects_symlinked_directory(self) -> None:
        bridge = Path(os.path.realpath(self._tmp.name))
        victim = bridge / "victim-dir"
        victim.mkdir(mode=0o755)
        (bridge / "nonces").symlink_to(victim, target_is_directory=True)

        with self.assertRaises(RuntimeError):
            helper.mint_send_nonce("+14155551234", "hello", "iMessage")

        self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o755)
        self.assertEqual(list(victim.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
