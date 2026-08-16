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


def make_segmented_attributed_blob(*segments: bytes) -> bytes:
    header = b"streamtyped\x00\x00\x00\x00\x00"
    body = bytearray(header)
    for segment in segments:
        body += b"NSString\x01\x2b"
        if len(segment) < 0x80:
            body.append(len(segment))
        else:
            body.append(0x81)
            body += struct.pack("<H", len(segment))
        body += segment
    return bytes(body)


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

    def test_data_detected_span_segments_are_joined(self) -> None:
        blob = make_segmented_attributed_blob(
            b"Call me at ",
            b"(415) 555-0123",
        )
        self.assertEqual(
            helper.decode_attributed_body(blob),
            "Call me at (415) 555-0123",
        )

    def test_data_detector_metadata_after_gap_is_not_appended(self) -> None:
        blob = (
            make_segmented_attributed_blob(b"Call me at ", b"(415) 555-0123")
            + b"attribute-run"
            + make_segmented_attributed_blob(b"tel:+14155550123")[16:]
        )
        self.assertEqual(
            helper.decode_attributed_body(blob),
            "Call me at (415) 555-0123",
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


class ProductModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="product-mode-test-")
        self.addCleanup(self._tmp.cleanup)
        self._saved_env = {}
        for var in ("IMESSAGE_PRODUCT_ID", "IMESSAGE_POLICY_DIR", "IMESSAGE_SEND_GATE_PATH",
                    "IMESSAGE_CONFIRM_HELPER_PATH", "COWORK_IMESSAGE_READ_POLICY"):
            self._saved_env[var] = os.environ.get(var)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        for var, value in self._saved_env.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value

    def test_missing_read_policy_defaults_to_allowlist_in_product_mode(self) -> None:
        """Verify that product mode defaults to allowlist when read_policy.txt is missing."""
        policy_dir = Path(self._tmp.name)
        policy_dir.mkdir(parents=True, exist_ok=True)
        
        old_wrapper_mode = helper.WRAPPER_MODE
        old_policy_root = helper.POLICY_ROOT
        old_read_policy_path = helper.READ_POLICY_PATH
        
        try:
            helper.WRAPPER_MODE = "product"
            helper.POLICY_ROOT = policy_dir
            helper.READ_POLICY_PATH = policy_dir / "read_policy.txt"
            
            policy = helper.load_privacy_policy()
            self.assertEqual(policy.mode, "allowlist")
        finally:
            helper.WRAPPER_MODE = old_wrapper_mode
            helper.POLICY_ROOT = old_policy_root
            helper.READ_POLICY_PATH = old_read_policy_path

    def test_missing_read_policy_defaults_to_blocklist_in_baked_mode(self) -> None:
        """Verify that baked mode defaults to blocklist when read_policy.txt is missing."""
        policy_dir = Path(self._tmp.name)
        policy_dir.mkdir(parents=True, exist_ok=True)
        
        old_wrapper_mode = helper.WRAPPER_MODE
        old_policy_root = helper.POLICY_ROOT
        old_read_policy_path = helper.READ_POLICY_PATH
        
        try:
            helper.WRAPPER_MODE = "baked"
            helper.POLICY_ROOT = policy_dir
            helper.READ_POLICY_PATH = policy_dir / "read_policy.txt"
            
            policy = helper.load_privacy_policy()
            self.assertEqual(policy.mode, "blocklist")
        finally:
            helper.WRAPPER_MODE = old_wrapper_mode
            helper.POLICY_ROOT = old_policy_root
            helper.READ_POLICY_PATH = old_read_policy_path

    def test_policy_file_permission_rejection_in_product_mode(self) -> None:
        """Verify that policy files with bad permissions are rejected in product mode."""
        policy_dir = Path(self._tmp.name)
        policy_dir.mkdir(parents=True, exist_ok=True)
        
        blocked_file = policy_dir / "blocked_chats.txt"
        blocked_file.write_text("+14155551234\n")
        blocked_file.chmod(0o664)
        
        old_wrapper_mode = helper.WRAPPER_MODE
        old_blocklist_path = helper.BLOCKLIST_PATH
        
        try:
            helper.WRAPPER_MODE = "product"
            helper.BLOCKLIST_PATH = blocked_file
            
            with mock.patch.object(helper, "log") as mock_log:
                policy = helper.load_privacy_policy()
                self.assertEqual(len(policy.blocklist), 0)
                
                logged = " ".join(str(call.args[0]) for call in mock_log.call_args_list)
                self.assertIn("group/world-writable", logged)
        finally:
            helper.WRAPPER_MODE = old_wrapper_mode
            helper.BLOCKLIST_PATH = old_blocklist_path

    def test_policy_file_accepts_correct_permissions_in_product_mode(self) -> None:
        """Verify that policy files with correct permissions are loaded in product mode."""
        policy_dir = Path(self._tmp.name)
        policy_dir.mkdir(parents=True, exist_ok=True)
        
        allowed_file = policy_dir / "allowed_chats.txt"
        allowed_file.write_text("+14155551234\n")
        allowed_file.chmod(0o600)
        
        old_wrapper_mode = helper.WRAPPER_MODE
        old_allowlist_path = helper.ALLOWLIST_PATH
        
        try:
            helper.WRAPPER_MODE = "product"
            helper.ALLOWLIST_PATH = allowed_file
            
            policy = helper.load_privacy_policy()
            self.assertEqual(len(policy.allowlist), 1)
        finally:
            helper.WRAPPER_MODE = old_wrapper_mode
            helper.ALLOWLIST_PATH = old_allowlist_path

    def test_action_status_includes_product_fields(self) -> None:
        """Verify that action_status returns the new product fields."""
        status = helper.action_status({}, None, {}, helper.load_privacy_policy())
        
        self.assertIn("product_id", status)
        self.assertIn("wrapper_mode", status)
        self.assertIn("policy_dir", status)
        
        self.assertIsInstance(status["product_id"], str)
        self.assertIsInstance(status["wrapper_mode"], str)
        self.assertIsInstance(status["policy_dir"], str)
        
        self.assertIn(status["wrapper_mode"], ("product", "baked"))


class SendGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="grokbot-nonce-test-")
        self.addCleanup(self._tmp.cleanup)
        self._old_bridge_new = os.environ.get("IMESSAGE_BRIDGE_DIR")
        self._old_bridge_old = os.environ.get("COWORK_IMESSAGE_BRIDGE_DIR")
        tmp_path = os.path.realpath(self._tmp.name)
        os.environ["IMESSAGE_BRIDGE_DIR"] = tmp_path
        os.environ["COWORK_IMESSAGE_BRIDGE_DIR"] = tmp_path
        self.addCleanup(self._restore_bridge)

    def _restore_bridge(self) -> None:
        if self._old_bridge_new is None:
            os.environ.pop("IMESSAGE_BRIDGE_DIR", None)
        else:
            os.environ["IMESSAGE_BRIDGE_DIR"] = self._old_bridge_new
        if self._old_bridge_old is None:
            os.environ.pop("COWORK_IMESSAGE_BRIDGE_DIR", None)
        else:
            os.environ["COWORK_IMESSAGE_BRIDGE_DIR"] = self._old_bridge_old

    def test_nonce_round_trip_and_replay_rejection(self) -> None:
        nonce = helper.mint_send_nonce("+14155551234", "hello", "iMessage")
        nonce_path = (
            Path(os.environ["IMESSAGE_BRIDGE_DIR"]) / "nonces" / f"{nonce}.json"
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
        nonce_dir = Path(os.environ["IMESSAGE_BRIDGE_DIR"]) / "nonces"
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
