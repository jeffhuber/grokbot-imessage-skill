from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests._helper_loader import REPO_ROOT, helper


class BridgeDirMixin:
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="grokbot-imessage-test-")
        self.addCleanup(self._tmp.cleanup)
        self._old_bridge = os.environ.get("COWORK_IMESSAGE_BRIDGE_DIR")
        os.environ["COWORK_IMESSAGE_BRIDGE_DIR"] = self._tmp.name
        self.addCleanup(self._restore_bridge)

    def _restore_bridge(self) -> None:
        if self._old_bridge is None:
            os.environ.pop("COWORK_IMESSAGE_BRIDGE_DIR", None)
        else:
            os.environ["COWORK_IMESSAGE_BRIDGE_DIR"] = self._old_bridge


class SendConfirmationTests(BridgeDirMixin, unittest.TestCase):
    def _nonce(self, to: str, text: str, service: str = "iMessage") -> str:
        return helper.mint_send_nonce(to, text, service)

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
        self.assertEqual(payload["to"], "+14155551234")
        self.assertEqual(payload["resolved_name"], "Alice Example")
        self.assertEqual(payload["text"], "all of this text must be visible")
        self.assertNotIn("all of this text", " ".join(call.args[0]))

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
            response_dir = Path(td)
            with mock.patch.object(helper, "RESPONSES_DIR", response_dir):
                helper.write_response("abc123", {"ok": True, "text": "private"})

            response = response_dir / "response-abc123.json"
            self.assertTrue(response.exists())
            self.assertEqual(stat.S_IMODE(response.stat().st_mode), 0o600)
            self.assertEqual(list(response_dir.glob("*.tmp")), [])

    def test_response_reaper_keeps_fresh_and_removes_stale(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-response-test-") as td:
            response_dir = Path(td)
            fresh = response_dir / "response-fresh.json"
            stale = response_dir / "response-stale.json"
            fresh.write_text("{}")
            stale.write_text("{}")
            old = time.time() - helper.RESPONSE_TTL_S - 10
            os.utime(stale, (old, old))

            with mock.patch.object(helper, "RESPONSES_DIR", response_dir):
                helper.reap_expired_responses()

            self.assertTrue(fresh.exists())
            self.assertFalse(stale.exists())

    def test_log_is_mode_600_and_rotated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-log-test-") as td:
            log_path = Path(td) / "log.txt"
            log_path.write_text("x" * 32)
            with mock.patch.object(helper, "LOG_PATH", log_path), mock.patch.object(
                helper, "LOG_MAX_BYTES", 16
            ):
                helper.log("fresh diagnostic")

            self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)
            self.assertIn("fresh diagnostic", log_path.read_text())
            self.assertEqual((Path(td) / "log.txt.1").read_text(), "x" * 32)

    def test_personal_blocklist_is_gitignored(self) -> None:
        gitignore = (REPO_ROOT / ".gitignore").read_text().splitlines()
        self.assertIn("contacts/blocked_chats.txt", gitignore)


if __name__ == "__main__":
    unittest.main()
