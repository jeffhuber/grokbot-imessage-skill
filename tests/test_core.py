from __future__ import annotations

import json
import os
import stat
import subprocess
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests._helper_loader import REPO_ROOT, helper


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

    def test_read_policy_permission_rejection_in_product_mode(self) -> None:
        """Verify that group-writable read_policy.txt is rejected in product mode."""
        policy_dir = Path(self._tmp.name)
        policy_dir.mkdir(parents=True, exist_ok=True)

        read_policy_file = policy_dir / "read_policy.txt"
        read_policy_file.write_text("blocklist\n")
        read_policy_file.chmod(0o664)

        old_wrapper_mode = helper.WRAPPER_MODE
        old_policy_root = helper.POLICY_ROOT
        old_read_policy_path = helper.READ_POLICY_PATH

        try:
            helper.WRAPPER_MODE = "product"
            helper.POLICY_ROOT = policy_dir
            helper.READ_POLICY_PATH = read_policy_file

            with mock.patch.object(helper, "log") as mock_log:
                policy = helper.load_privacy_policy()
                self.assertEqual(policy.mode, "allowlist")
                logged = " ".join(str(call.args[0]) for call in mock_log.call_args_list)
                self.assertIn("read_policy.txt rejected", logged)
                self.assertIn("group/world-writable", logged)
        finally:
            helper.WRAPPER_MODE = old_wrapper_mode
            helper.POLICY_ROOT = old_policy_root
            helper.READ_POLICY_PATH = old_read_policy_path

    def test_env_overrides_honored_at_import(self) -> None:
        """Verify that env vars are honored when helper is imported."""
        import importlib.util

        policy_dir = Path(self._tmp.name) / "custom_policy"
        policy_dir.mkdir(parents=True, exist_ok=True)
        blocked_file = policy_dir / "blocked_chats.txt"
        blocked_file.write_text("+14155551234\n")
        blocked_file.chmod(0o600)

        send_gate_copy = Path(self._tmp.name) / "test_send_gate.py"
        send_gate_copy.write_text((REPO_ROOT / "bin" / "send_gate.py").read_text())
        confirm_helper_path = Path(self._tmp.name) / "test-confirm"
        confirm_helper_path.write_text("#!/bin/sh\n")

        env_vars = {
            "IMESSAGE_POLICY_DIR": str(policy_dir),
            "IMESSAGE_SEND_GATE_PATH": str(send_gate_copy),
            "IMESSAGE_CONFIRM_HELPER_PATH": str(confirm_helper_path),
            "IMESSAGE_PRODUCT_ID": "test-custom-product",
            "IMESSAGE_BRIDGE_DIR": self._tmp.name,
        }

        env_backup = {k: os.environ.get(k) for k in env_vars}
        for k, v in env_vars.items():
            os.environ[k] = v

        try:
            spec = importlib.util.spec_from_file_location("test_helper_fresh", REPO_ROOT / "bin" / "helper.py")
            self.assertIsNotNone(spec, "spec_from_file_location returned None")
            self.assertIsNotNone(spec.loader, "spec.loader is None")

            fresh_helper = importlib.util.module_from_spec(spec)
            sys.modules["test_helper_fresh"] = fresh_helper

            try:
                spec.loader.exec_module(fresh_helper)

                self.assertEqual(str(fresh_helper.POLICY_ROOT), str(policy_dir))
                self.assertEqual(str(fresh_helper.SEND_GATE_PATH), str(send_gate_copy))
                self.assertEqual(str(fresh_helper.CONFIRM_HELPER_PATH), str(confirm_helper_path))
                self.assertEqual(fresh_helper.WRAPPER_MODE, "product")
                self.assertEqual(fresh_helper.PRODUCT_ID, "test-custom-product")

                status = fresh_helper.action_status({}, None, {}, fresh_helper.load_privacy_policy())
                self.assertEqual(status["product_id"], "test-custom-product")
                self.assertEqual(status["wrapper_mode"], "product")
                self.assertIsNone(status["launchd_label"])
                self.assertEqual(status["policy_dir"], str(policy_dir))
            finally:
                sys.modules.pop("test_helper_fresh", None)
        finally:
            for k, v in env_backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


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


class RoleGatingTests(unittest.TestCase):
    """Test IMESSAGE_BRIDGE_ROLE role-based action gating (CORE-5b)."""

    def test_manager_allowed_actions_in_status(self) -> None:
        """Manager role reports correct allowed_actions in status response."""
        script = """
import os
import sys
os.environ["IMESSAGE_BRIDGE_DIR"] = "/tmp/test-bridge"
os.environ["IMESSAGE_BRIDGE_ROLE"] = "manager"
sys.path.insert(0, "tests")
from _helper_loader import helper
result = helper.action_status({}, None, {}, [])
assert result["bridge_role"] == "manager", f"Expected manager, got {result['bridge_role']}"
assert "list_chats" in result["allowed_actions"], "list_chats should be in allowed_actions"
assert "review" not in result["allowed_actions"], "review should not be in allowed_actions"
print("OK")
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("OK", completed.stdout)

    def test_host_allowed_actions_in_status(self) -> None:
        """Host role reports correct allowed_actions in status response."""
        script = """
import os
import sys
os.environ["IMESSAGE_BRIDGE_DIR"] = "/tmp/test-bridge"
os.environ["IMESSAGE_BRIDGE_ROLE"] = "host"
sys.path.insert(0, "tests")
from _helper_loader import helper
result = helper.action_status({}, None, {}, [])
assert result["bridge_role"] == "host", f"Expected host, got {result['bridge_role']}"
assert "review" in result["allowed_actions"], "review should be in allowed_actions"
assert "send" in result["allowed_actions"], "send should be in allowed_actions"
assert len(result["allowed_actions"]) == 8, f"Expected 8 actions, got {len(result['allowed_actions'])}"
print("OK")
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("OK", completed.stdout)


class SendPolicyTests(unittest.TestCase):
    """Test send_policy.json gating (CORE-5b)."""

    def test_send_policy_disabled_blocks_preview(self) -> None:
        """send_policy.json enabled=false blocks send_preview."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = Path(os.path.realpath(tmpdir))
            policy_dir = bridge / "policy"
            policy_dir.mkdir(mode=0o700)
            policy_file = policy_dir / "send_policy.json"
            policy_file.write_text(json.dumps({"schema": 1, "enabled": False, "acknowledged_at": None}))
            policy_file.chmod(0o600)

            script = f"""
import os
import sys
os.environ["IMESSAGE_BRIDGE_DIR"] = "{bridge}"
os.environ["IMESSAGE_POLICY_DIR"] = "{policy_dir}"
sys.path.insert(0, "tests")
from _helper_loader import helper
try:
    helper.action_send_preview({{"to": "+14155551234", "text": "hello"}}, None, {{}}, [])
    print("ERROR: should have raised")
    sys.exit(1)
except ValueError as e:
    if "disabled by policy" in str(e):
        print("OK")
    else:
        print(f"ERROR: wrong error: {{e}}")
        sys.exit(1)
"""
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("OK", completed.stdout)

    def test_send_policy_enabled_allows_preview(self) -> None:
        """send_policy.json enabled=true allows send_preview to mint nonce."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = Path(os.path.realpath(tmpdir))
            policy_dir = bridge / "policy"
            policy_dir.mkdir(mode=0o700)
            (bridge / "control" / "requests").mkdir(parents=True, mode=0o700)
            (bridge / "control" / "responses").mkdir(parents=True, mode=0o700)
            policy_file = policy_dir / "send_policy.json"
            policy_file.write_text(json.dumps({"schema": 1, "enabled": True, "acknowledged_at": "2026-01-01"}))
            policy_file.chmod(0o600)

            script = f"""
import os
import sys
os.environ["IMESSAGE_BRIDGE_DIR"] = "{bridge}"
os.environ["IMESSAGE_POLICY_DIR"] = "{policy_dir}"
sys.path.insert(0, "tests")
from _helper_loader import helper
result = helper.action_send_preview({{"to": "+14155551234", "text": "hello"}}, None, {{}}, [])
assert "send_nonce" in result, "send_nonce should be in result"
print("OK")
"""
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("OK", completed.stdout)

    def test_product_mode_default_policy_dir_blocks_preview(self) -> None:
        """Product mode enforces the default contacts/send_policy.json path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = Path(os.path.realpath(tmpdir))
            policy_dir = bridge / "contacts"
            policy_dir.mkdir(mode=0o700)
            policy_file = policy_dir / "send_policy.json"
            policy_file.write_text(json.dumps({"schema": 1, "enabled": False, "acknowledged_at": None}))
            policy_file.chmod(0o600)

            script = f"""
import os
import sys
os.environ["IMESSAGE_BRIDGE_DIR"] = "{bridge}"
os.environ["IMESSAGE_PRODUCT_ID"] = "grokbot-imessage"
sys.path.insert(0, "tests")
from _helper_loader import helper
try:
    helper.action_send_preview({{"to": "+14155551234", "text": "hello"}}, None, {{}}, [])
    print("ERROR: should have raised")
    sys.exit(1)
except ValueError as e:
    if "disabled by policy" in str(e):
        print("OK")
    else:
        print(f"ERROR: wrong error: {{e}}")
        sys.exit(1)
"""
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("OK", completed.stdout)


class ManagerModeTests(unittest.TestCase):
    """Test manager mode special cases (CORE-5b)."""

    def test_manager_contacts_lookup_unfiltered(self) -> None:
        """Manager mode returns all contacts regardless of blocklist."""
        script = """
import os
import sys
os.environ["IMESSAGE_BRIDGE_DIR"] = "/tmp/test-bridge"
os.environ["IMESSAGE_BRIDGE_ROLE"] = "manager"
sys.path.insert(0, "tests")
from _helper_loader import helper
contacts = {"1234567890": "Blocked User"}
policy = helper.PrivacyPolicy(mode="blocklist", blocklist=("1234567890",), allowlist=())
result = helper.action_contacts_lookup({"name": "Blocked"}, None, contacts, policy)
assert result["match_count"] == 1, f"Expected 1 match, got {result['match_count']}"
assert result["matches"][0]["name"] == "Blocked User", "Should see blocked contact"
print("OK")
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("OK", completed.stdout)


class PerBridgeLockTests(unittest.TestCase):
    """Test per-bridge advisory lock (CORE-6)."""

    def test_concurrent_workers_serialize_on_same_bridge(self) -> None:
        """Two workers on the same bridge process each request exactly once."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = Path(os.path.realpath(tmpdir))
            control = bridge / "control"
            requests = control / "requests"
            responses = control / "responses"

            # Set up bridge directories
            control.mkdir(mode=0o700)
            requests.mkdir(mode=0o700)
            responses.mkdir(mode=0o700)

            # Plant two status requests
            req1 = requests / "request-worker1-test.json"
            req2 = requests / "request-worker2-test.json"
            req1.write_text(json.dumps({"id": "worker1-test", "action": "status", "params": {}}))
            req2.write_text(json.dumps({"id": "worker2-test", "action": "status", "params": {}}))
            req1.chmod(0o600)
            req2.chmod(0o600)

            # Launch two helper processes concurrently
            script = f"""
import os
import sys
os.environ["IMESSAGE_BRIDGE_DIR"] = "{bridge}"
sys.path.insert(0, "tests")
from _helper_loader import helper
helper.main()
"""
            procs = []
            for _ in range(2):
                proc = subprocess.Popen(
                    [sys.executable, "-c", script],
                    cwd=REPO_ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                procs.append(proc)

            # Wait for both to complete
            results = []
            for proc in procs:
                stdout, stderr = proc.communicate(timeout=10)
                results.append((proc.returncode, stdout, stderr))

            # Both should succeed
            for i, (rc, stdout, stderr) in enumerate(results):
                self.assertEqual(rc, 0, f"Worker {i} failed: {stderr}")

            # Both requests should have been consumed
            remaining_requests = list(requests.glob("request-*.json"))
            self.assertEqual(len(remaining_requests), 0,
                           f"Expected 0 remaining requests, found {len(remaining_requests)}")

            # Both responses should exist
            response_files = list(responses.glob("response-*.json"))
            self.assertEqual(len(response_files), 2,
                           f"Expected 2 responses, found {len(response_files)}")

            # Verify each response matches a request ID
            response_ids = set()
            for response_file in response_files:
                response_data = json.loads(response_file.read_text())
                self.assertTrue(response_data.get("ok"),
                              f"Response {response_file.name} not ok: {response_data.get('error')}")
                response_ids.add(response_data["id"])

            self.assertEqual(response_ids, {"worker1-test", "worker2-test"},
                           "Response IDs should match request IDs")

    def test_lock_timeout_on_held_lock(self) -> None:
        """Lock acquisition times out when another process holds the lock."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = Path(os.path.realpath(tmpdir))
            control = bridge / "control"
            control.mkdir(mode=0o700)

            # Script that holds the lock for 10 seconds
            holder_script = f"""
import os
import sys
import time
os.environ["IMESSAGE_BRIDGE_DIR"] = "{bridge}"
sys.path.insert(0, "tests")
from _helper_loader import helper

# Acquire lock and hold it
with helper._private_directory_fd(helper.LOG_PATH.parent) as control_fd:
    lock_fd = helper._acquire_bridge_lock(control_fd)
    try:
        print("LOCKED", flush=True)
        time.sleep(10)
    finally:
        os.close(lock_fd)
"""

            # Start holder process
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_script],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Wait for lock to be acquired
            import select
            ready, _, _ = select.select([holder.stdout], [], [], 5.0)
            self.assertTrue(ready, "Holder process didn't acquire lock")
            line = holder.stdout.readline()
            self.assertEqual(line.strip(), "LOCKED")

            try:
                # Now try to acquire the lock with timeout
                waiter_script = f"""
import os
import sys
os.environ["IMESSAGE_BRIDGE_DIR"] = "{bridge}"
sys.path.insert(0, "tests")
from _helper_loader import helper

try:
    with helper._private_directory_fd(helper.LOG_PATH.parent) as control_fd:
        lock_fd = helper._acquire_bridge_lock(control_fd, timeout_s=1.0)
        os.close(lock_fd)
    print("ERROR: should have timed out")
    sys.exit(1)
except RuntimeError as e:
    if "could not acquire bridge lock" in str(e):
        print("OK: timed out as expected")
        sys.exit(0)
    else:
        print(f"ERROR: wrong error: {{e}}")
        sys.exit(1)
"""
                waiter = subprocess.run(
                    [sys.executable, "-c", waiter_script],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=5,
                )

                self.assertEqual(waiter.returncode, 0, waiter.stderr)
                self.assertIn("OK: timed out as expected", waiter.stdout)
            finally:
                holder.terminate()
                try:
                    holder.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    holder.kill()
                    holder.communicate(timeout=2)


if __name__ == "__main__":
    unittest.main()
