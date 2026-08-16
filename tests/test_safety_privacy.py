from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests._helper_loader import REPO_ROOT, helper

# Load send_gate module directly for tests that need to check its internals
import importlib.util as _importlib_util
_send_gate_spec = _importlib_util.spec_from_file_location(
    "send_gate", REPO_ROOT / "bin" / "send_gate.py"
)
_send_gate = _importlib_util.module_from_spec(_send_gate_spec)
_send_gate_spec.loader.exec_module(_send_gate)


class BridgeDirMixin:
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="grokbot-imessage-test-")
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


class SendConfirmationTests(BridgeDirMixin, unittest.TestCase):
    def _nonce(self, to: str, text: str, service: str = "iMessage") -> str:
        return helper.mint_send_nonce(to, text, service)

    def test_bridge_dir_fails_closed_without_env_var(self) -> None:
        old_env_new = os.environ.pop("IMESSAGE_BRIDGE_DIR", None)
        old_env_old = os.environ.pop("COWORK_IMESSAGE_BRIDGE_DIR", None)
        try:
            with self.assertRaisesRegex(
                RuntimeError, "IMESSAGE_BRIDGE_DIR is required"
            ):
                _send_gate._bridge_dir()
            with self.assertRaisesRegex(
                RuntimeError, "IMESSAGE_BRIDGE_DIR is required"
            ):
                _send_gate.mint_send_nonce("+14155551234", "test", "iMessage")
        finally:
            if old_env_new is not None:
                os.environ["IMESSAGE_BRIDGE_DIR"] = old_env_new
            if old_env_old is not None:
                os.environ["COWORK_IMESSAGE_BRIDGE_DIR"] = old_env_old

    def test_empty_new_bridge_dir_does_not_fall_back_to_old_name(self) -> None:
        os.environ["IMESSAGE_BRIDGE_DIR"] = ""
        os.environ["COWORK_IMESSAGE_BRIDGE_DIR"] = self._tmp.name
        with self.assertRaisesRegex(RuntimeError, "IMESSAGE_BRIDGE_DIR is required"):
            _send_gate._bridge_dir()
        with self.assertRaisesRegex(RuntimeError, "IMESSAGE_BRIDGE_DIR is required"):
            _send_gate.mint_send_nonce("+14155551234", "test", "iMessage")

    def test_old_env_var_alias_still_works(self) -> None:
        # Test that the old COWORK_IMESSAGE_BRIDGE_DIR name still works
        os.environ.pop("IMESSAGE_BRIDGE_DIR", None)
        os.environ["COWORK_IMESSAGE_BRIDGE_DIR"] = self._tmp.name
        try:
            bridge_dir = _send_gate._bridge_dir()
            self.assertEqual(os.path.realpath(str(bridge_dir)), os.path.realpath(self._tmp.name))
        finally:
            os.environ["IMESSAGE_BRIDGE_DIR"] = self._tmp.name

    def test_full_payload_and_raw_recipient_reach_confirmation(self) -> None:
        to = "+14155551234"
        text = "prefix " + ("x" * 600) + " hidden suffix"
        nonce = self._nonce(to, text)
        contacts = {"4155551234": "Alice Example"}

        with mock.patch.object(
            helper, "_run_send_confirmation", return_value=True
        ) as confirm, mock.patch.object(
            helper, "_run_osascript", return_value=(0, "", "")
        ) as send:
            helper.action_send(
                {"to": to, "text": text, "send_nonce": nonce},
                None,
                contacts,
                [],
            )

        confirm.assert_called_once_with(
            to=to,
            resolved_name="Alice Example",
            service="iMessage",
            text=text,
        )
        send.assert_called_once()

    def test_cancelled_confirmation_never_calls_messages(self) -> None:
        to = "+14155551234"
        text = "do not send"
        nonce = self._nonce(to, text)

        with mock.patch.object(
            helper, "_run_send_confirmation", return_value=False
        ), mock.patch.object(helper, "_run_osascript") as send:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                helper.action_send(
                    {"to": to, "text": text, "send_nonce": nonce},
                    None,
                    {},
                    [],
                )

        send.assert_not_called()

    def test_confirmation_helper_receives_json_on_stdin(self) -> None:
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(helper.subprocess, "run", return_value=completed) as run:
            approved = helper._run_send_confirmation(
                to="+14155551234",
                resolved_name="Alice Example",
                service="iMessage",
                text="all of this text must be visible",
            )

        self.assertTrue(approved)
        call = run.call_args
        self.assertEqual(call.args[0], [str(helper.CONFIRM_HELPER_PATH)])
        payload = json.loads(call.kwargs["input"])
        self.assertEqual(payload["client_name"], "Grok Bot")
        self.assertEqual(payload["to"], "+14155551234")
        self.assertEqual(payload["resolved_name"], "Alice Example")
        self.assertEqual(payload["text"], "all of this text must be visible")
        self.assertNotIn("all of this text", " ".join(call.args[0]))

    def test_confirmation_helper_path_can_come_from_environment(self) -> None:
        script = """
import importlib.util
import sys
path = sys.argv[1]
spec = importlib.util.spec_from_file_location("helper_env_probe", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
print(module.CONFIRM_HELPER_PATH)
"""
        expected = Path("/tmp/Bridge Pro.app/Contents/Helpers/imessage-confirm")
        env = {
            **os.environ,
            "IMESSAGE_CONFIRM_HELPER_PATH": str(expected),
            "IMESSAGE_BRIDGE_DIR": self._tmp.name,
        }
        result = subprocess.run(
            [sys.executable, "-c", script, str(REPO_ROOT / "bin" / "helper.py")],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), str(expected))

    def test_confirmation_helper_fails_closed(self) -> None:
        for returncode, expected in ((1, False), (3, False)):
            completed = mock.Mock(returncode=returncode, stdout="", stderr="")
            with mock.patch.object(helper.subprocess, "run", return_value=completed):
                self.assertEqual(
                    helper._run_send_confirmation(
                        to="+14155551234",
                        resolved_name="",
                        service="iMessage",
                        text="hello",
                    ),
                    expected,
                )

        completed = mock.Mock(returncode=2, stdout="", stderr="bad input")
        with mock.patch.object(helper.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "confirmation helper failed"):
                helper._run_send_confirmation(
                    to="+14155551234",
                    resolved_name="",
                    service="iMessage",
                    text="hello",
                )

    def test_native_confirmation_source_defaults_to_cancel(self) -> None:
        source = (REPO_ROOT / "bin" / "confirm_imessage_send.m").read_text()
        cancel_pos = source.index('addButtonWithTitle:@"Cancel"')
        send_pos = source.index('addButtonWithTitle:@"Send"')
        self.assertLess(cancel_pos, send_pos)
        self.assertIn('cancelButton.keyEquivalent = @"\\r"', source)
        self.assertIn('sendButton.keyEquivalent = @""', source)
        self.assertIn("timerWithTimeInterval:kConfirmationTimeoutSeconds", source)
        self.assertIn("forMode:NSModalPanelRunLoopMode", source)
        self.assertIn('@"client_name"', source)
        self.assertNotIn("scheduledTimerWithTimeInterval", source)


class SensitiveArtifactTests(unittest.TestCase):
    def test_attributed_body_failure_log_contains_no_blob_bytes(self) -> None:
        blob = b"streamtyped-secret-message-material"
        with mock.patch.object(helper, "log") as log:
            self.assertEqual(helper._attributed_fail(blob, "test failure"), "")

        message = log.call_args.args[0]
        self.assertIn("bytes=", message)
        self.assertNotIn(blob.hex(), message)
        self.assertNotIn("secret", message)

    def test_responses_are_mode_600_and_atomically_written(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-response-test-") as td:
            bridge = Path(os.path.realpath(td))
            bridge.chmod(0o700)
            control = bridge / "control"
            control.mkdir(mode=0o700)
            response_dir = control / "responses"
            response_dir.mkdir(mode=0o700)
            with mock.patch.object(helper, "BRIDGE_ROOT", bridge), mock.patch.object(
                helper, "RESPONSES_DIR", response_dir
            ):
                helper.write_response("abc123", {"ok": True, "text": "private"})

            response = response_dir / "response-abc123.json"
            self.assertTrue(response.exists())
            self.assertEqual(stat.S_IMODE(response_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(response.stat().st_mode), 0o600)
            self.assertEqual(list(response_dir.glob("*.tmp")), [])

    def test_main_accepts_private_control_and_queue_directories(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-control-test-") as td:
            bridge = Path(os.path.realpath(td))
            bridge.chmod(0o700)
            control_dir = bridge / "control"
            requests_dir = control_dir / "requests"
            responses_dir = control_dir / "responses"
            for path in (requests_dir, responses_dir):
                path.mkdir(parents=True, mode=0o700)
            control_dir.chmod(0o700)

            with mock.patch.object(helper, "BRIDGE_ROOT", bridge), mock.patch.object(
                helper, "LOG_PATH", control_dir / "log.txt"
            ), mock.patch.object(
                helper, "REQUESTS_DIR", requests_dir
            ), mock.patch.object(helper, "RESPONSES_DIR", responses_dir), mock.patch.object(
                helper, "load_blocklist", return_value=[]
            ), mock.patch.object(helper, "reap_expired_nonces"):
                helper.main()

            for path in (control_dir, requests_dir, responses_dir):
                with self.subTest(path=path):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_response_reaper_keeps_fresh_and_removes_stale(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-response-test-") as td:
            bridge = Path(os.path.realpath(td))
            bridge.chmod(0o700)
            control = bridge / "control"
            control.mkdir(mode=0o700)
            response_dir = control / "responses"
            response_dir.mkdir(mode=0o700)
            fresh = response_dir / "response-fresh.json"
            stale = response_dir / "response-stale.json"
            legacy = response_dir / "response-legacy.json"
            fresh.write_text("{}")
            stale.write_text("{}")
            legacy.write_text("{}")
            fresh.chmod(0o600)
            stale.chmod(0o600)
            legacy.chmod(0o644)
            old = time.time() - helper.RESPONSE_TTL_S - 10
            os.utime(stale, (old, old))
            os.utime(legacy, (old, old))

            with mock.patch.object(helper, "BRIDGE_ROOT", bridge), mock.patch.object(
                helper, "RESPONSES_DIR", response_dir
            ):
                helper.reap_expired_responses()

            self.assertTrue(fresh.exists())
            self.assertFalse(stale.exists())
            self.assertFalse(legacy.exists())

    def test_response_reaper_tolerates_missing_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-response-missing-test-") as td:
            bridge = Path(os.path.realpath(td))
            bridge.chmod(0o700)
            control = bridge / "control"
            control.mkdir(mode=0o700)

            with mock.patch.object(helper, "BRIDGE_ROOT", bridge), mock.patch.object(
                helper, "RESPONSES_DIR", control / "responses"
            ):
                helper.reap_expired_responses()

    def test_log_is_mode_600_and_rotated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-log-test-") as td:
            bridge = Path(os.path.realpath(td))
            bridge.chmod(0o700)
            control = bridge / "control"
            control.mkdir(mode=0o700)
            log_path = control / "log.txt"
            log_path.write_text("x" * 32)
            log_path.chmod(0o600)
            first_archive = control / "log.txt.1"
            first_archive.write_text("older")
            first_archive.chmod(0o600)
            with mock.patch.object(helper, "BRIDGE_ROOT", bridge), mock.patch.object(
                helper, "LOG_PATH", log_path
            ), mock.patch.object(
                helper, "LOG_MAX_BYTES", 16
            ):
                helper.log("fresh diagnostic")

            self.assertEqual(stat.S_IMODE(control.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)
            self.assertIn("fresh diagnostic", log_path.read_text())
            self.assertEqual(first_archive.read_text(), "x" * 32)
            self.assertEqual(stat.S_IMODE(first_archive.stat().st_mode), 0o600)
            second_archive = control / "log.txt.2"
            self.assertEqual(second_archive.read_text(), "older")
            self.assertEqual(stat.S_IMODE(second_archive.stat().st_mode), 0o600)

    def test_personal_blocklist_is_gitignored(self) -> None:
        gitignore = (REPO_ROOT / ".gitignore").read_text().splitlines()
        self.assertIn("contacts/blocked_chats.txt", gitignore)

    def test_log_rejects_symlinks_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-log-symlink-test-") as td:
            bridge = Path(os.path.realpath(td))
            bridge.chmod(0o700)
            control = bridge / "control"
            control.mkdir(mode=0o700)
            victim = bridge / "victim.txt"
            victim.write_text("unchanged\n")
            victim.chmod(0o600)
            (control / "log.txt").symlink_to(victim)

            with mock.patch.object(helper, "BRIDGE_ROOT", bridge), mock.patch.object(
                helper, "LOG_PATH", control / "log.txt"
            ):
                helper.log("must not follow")

            self.assertEqual(victim.read_text(), "unchanged\n")

    def test_log_rejects_symlinked_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-dir-symlink-test-") as td:
            bridge = Path(os.path.realpath(td))
            bridge.chmod(0o700)
            victim = bridge / "victim-dir"
            victim.mkdir(mode=0o755)
            (bridge / "control").symlink_to(victim, target_is_directory=True)

            with mock.patch.object(helper, "BRIDGE_ROOT", bridge), mock.patch.object(
                helper, "LOG_PATH", bridge / "control" / "log.txt"
            ):
                helper.log("must not follow")

            self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o755)
            self.assertFalse((victim / "log.txt").exists())

    def test_runtime_directory_permissions_are_not_repaired(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-dir-mode-test-") as td:
            bridge = Path(os.path.realpath(td))
            bridge.chmod(0o700)
            control = bridge / "control"
            control.mkdir(mode=0o755)

            with mock.patch.object(helper, "BRIDGE_ROOT", bridge), mock.patch.object(
                helper, "LOG_PATH", control / "log.txt"
            ), mock.patch.object(helper, "REQUESTS_DIR", control / "requests"), mock.patch.object(
                helper, "RESPONSES_DIR", control / "responses"
            ), self.assertRaises(helper.UnsafeRuntimePath):
                helper.main()

            self.assertEqual(stat.S_IMODE(control.stat().st_mode), 0o755)

    def test_bridge_root_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-root-symlink-test-") as td:
            parent = Path(os.path.realpath(td))
            victim = parent / "victim"
            victim.mkdir(mode=0o700)
            bridge = parent / "bridge"
            bridge.symlink_to(victim, target_is_directory=True)

            with mock.patch.object(helper, "BRIDGE_ROOT", bridge), self.assertRaises(
                helper.UnsafeRuntimePath
            ):
                with helper._private_directory_fd(bridge / "control", create=True):
                    pass

            self.assertEqual(list(victim.iterdir()), [])

    def test_response_writer_rejects_symlinked_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-response-symlink-test-") as td:
            bridge = Path(os.path.realpath(td))
            bridge.chmod(0o700)
            control = bridge / "control"
            control.mkdir(mode=0o700)
            victim = bridge / "victim-dir"
            victim.mkdir(mode=0o755)
            responses = control / "responses"
            responses.symlink_to(victim, target_is_directory=True)

            with mock.patch.object(helper, "BRIDGE_ROOT", bridge), mock.patch.object(
                helper, "RESPONSES_DIR", responses
            ), self.assertRaises(RuntimeError):
                helper.write_response("symlink", {"ok": True})

            self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o755)
            self.assertEqual(list(victim.iterdir()), [])

    def test_launchd_does_not_open_user_controlled_log_path(self) -> None:
        plist = (REPO_ROOT / "com.jeffhuber.grokbot-imessage.plist.template").read_text()
        self.assertNotIn("{{BRIDGE_ROOT}}/control/log.txt", plist)
        self.assertEqual(plist.count("<string>/dev/null</string>"), 2)


class Core5aTests(unittest.TestCase):
    """CORE-5a follow-ups: read_policy ownership, root/uid precedence, send_gate check, env-plumbing."""

    def test_read_policy_group_writable_does_not_switch_to_blocklist(self) -> None:
        """Item 1: group-writable read_policy.txt saying 'blocklist' must NOT flip policy."""
        with tempfile.TemporaryDirectory(prefix="core5a-read-policy-") as td:
            policy_dir = Path(os.path.realpath(td))
            policy_dir.chmod(0o700)
            read_policy = policy_dir / "read_policy.txt"
            read_policy.write_text("blocklist\n", encoding="utf-8")
            read_policy.chmod(0o664)  # group-writable

            with mock.patch.object(helper, "WRAPPER_MODE", "product"), mock.patch.object(
                helper, "POLICY_ROOT", policy_dir
            ), mock.patch.object(helper, "READ_POLICY_PATH", read_policy):
                policy = helper.load_privacy_policy()

            # Product mode rejects group-writable read_policy.txt → treats as missing → allowlist (fail-closed)
            self.assertEqual(policy.mode, "allowlist")

    def test_root_owned_allowlist_satisfies_uid_check(self) -> None:
        """Item 2: root-owned allowlist under product mode should satisfy uid check."""
        with tempfile.TemporaryDirectory(prefix="core5a-root-") as td:
            policy_dir = Path(os.path.realpath(td))
            policy_dir.chmod(0o700)
            allowlist_path = policy_dir / "allowed_chats.txt"
            allowlist_path.write_text("+14155551234\n", encoding="utf-8")
            allowlist_path.chmod(0o600)
            
            # Create a mock stat result that looks root-owned
            original_stat = allowlist_path.stat()
            fake_stat = mock.Mock()
            fake_stat.st_mode = stat.S_IFREG | 0o600
            fake_stat.st_uid = 0  # root
            fake_stat.st_size = original_stat.st_size
            
            # Patch Path.lstat at the module level where _load_list calls it
            with mock.patch('pathlib.Path.lstat', return_value=fake_stat):
                entries = helper._load_list(allowlist_path, require_uid_owner=True)
            
            # Root-owned should satisfy uid check (not be rejected)
            self.assertEqual(entries, ("+14155551234",))

    def test_send_gate_ownership_checked(self) -> None:
        """Item 3: _load_send_gate should check file ownership and permissions."""
        with tempfile.TemporaryDirectory(prefix="core5a-sendgate-") as td:
            gate_dir = Path(os.path.realpath(td))
            gate_dir.chmod(0o700)
            gate_path = gate_dir / "send_gate.py"
            gate_path.write_text("SEND_NONCE_TTL = 60\n", encoding="utf-8")
            gate_path.chmod(0o664)  # group-writable
            
            with mock.patch.object(helper, "SEND_GATE_PATH", gate_path):
                with self.assertRaisesRegex(RuntimeError, "must not be group/world-writable"):
                    helper._load_send_gate()

    def test_env_plumbing_product_mode(self) -> None:
        """Item 4: env vars should set product mode and derive paths correctly."""
        import importlib.util
        import sys
        
        with tempfile.TemporaryDirectory(prefix="core5a-env-") as td:
            policy_dir = Path(os.path.realpath(td))
            policy_dir.chmod(0o700)
            send_gate = policy_dir / "send_gate.py"
            send_gate.write_text("SEND_NONCE_TTL = 60\nclass SendGateError(Exception): pass\n"
                               "def mint_send_nonce(to, text, service): return 'nonce'\n"
                               "def consume_send_nonce(nonce, to, text, service): pass\n"
                               "def reap_expired_nonces(): pass\n", encoding="utf-8")
            send_gate.chmod(0o600)
            confirm_helper = policy_dir / "grokbot-imessage-confirm"
            confirm_helper.write_text("#!/bin/sh\n", encoding="utf-8")
            confirm_helper.chmod(0o700)
            
            test_env = {
                **os.environ,
                "IMESSAGE_POLICY_DIR": str(policy_dir),
                "IMESSAGE_SEND_GATE_PATH": str(send_gate),
                "IMESSAGE_CONFIRM_HELPER_PATH": str(confirm_helper),
                "IMESSAGE_PRODUCT_ID": "grokbot-imessage",
                "IMESSAGE_BRIDGE_DIR": td,
            }
            
            # Load helper under a fresh module name
            helper_path = REPO_ROOT / "bin" / "helper.py"
            module_name = f"helper_test_{id(self)}"
            spec = importlib.util.spec_from_file_location(module_name, helper_path)
            test_helper = importlib.util.module_from_spec(spec)
            
            # Execute with test environment
            old_environ = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(test_env)
                sys.modules[module_name] = test_helper
                spec.loader.exec_module(test_helper)
                
                # Verify product mode and paths
                self.assertEqual(test_helper.WRAPPER_MODE, "product")
                self.assertEqual(test_helper.PRODUCT_ID, "grokbot-imessage")
                self.assertEqual(
                    os.path.realpath(str(test_helper.POLICY_ROOT)),
                    os.path.realpath(str(policy_dir))
                )
                self.assertEqual(
                    os.path.realpath(str(test_helper.SEND_GATE_PATH)),
                    os.path.realpath(str(send_gate))
                )
                self.assertEqual(
                    os.path.realpath(str(test_helper.CONFIRM_HELPER_PATH)),
                    os.path.realpath(str(confirm_helper))
                )
            finally:
                os.environ.clear()
                os.environ.update(old_environ)
                if module_name in sys.modules:
                    del sys.modules[module_name]


if __name__ == "__main__":
    unittest.main()
