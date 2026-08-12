from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._helper_loader import REPO_ROOT, helper


class SQLiteBackupTests(unittest.TestCase):
    def test_copy_chatdb_uses_sqlite_backup_and_returns_consistent_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-db-test-") as td:
            source = Path(td) / "chat.db"
            with sqlite3.connect(str(source)) as conn:
                conn.execute("CREATE TABLE sample(value TEXT)")
                conn.execute("INSERT INTO sample VALUES ('hello')")

            with mock.patch.object(helper, "CHAT_DB_PATH", source):
                snapshot = helper.copy_chatdb()
            self.addCleanup(helper.cleanup_tmpdb, snapshot)

            with sqlite3.connect(str(snapshot)) as conn:
                self.assertEqual(conn.execute("SELECT value FROM sample").fetchone(), ("hello",))
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            self.assertFalse(Path(str(snapshot) + "-wal").exists())

    def test_copy_chatdb_includes_uncheckpointed_wal_rows(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-wal-test-") as td:
            source = Path(td) / "chat.db"
            with sqlite3.connect(str(source)) as writer:
                self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",))
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE sample(value TEXT)")
                writer.commit()
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                writer.execute("INSERT INTO sample VALUES ('from-live-wal')")
                writer.commit()
                self.assertGreater(Path(f"{source}-wal").stat().st_size, 0)

                with mock.patch.object(helper, "CHAT_DB_PATH", source):
                    snapshot = helper.copy_chatdb()
                self.addCleanup(helper.cleanup_tmpdb, snapshot)

            with sqlite3.connect(str(snapshot)) as conn:
                self.assertEqual(
                    conn.execute("SELECT value FROM sample").fetchall(),
                    [("from-live-wal",)],
                )
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone(), ("ok",))


class StatusContractTests(unittest.TestCase):
    def test_status_is_whitelisted_and_does_not_need_chat_db(self) -> None:
        self.assertIn("status", helper.ACTIONS)
        self.assertFalse(helper.action_status.needs_db)
        self.assertFalse(helper.action_status.needs_contacts)

    def test_status_request_does_not_load_messages_or_contacts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-status-test-") as td:
            request = Path(td) / "request-status.json"
            request.write_text(json.dumps({"id": "status", "action": "status", "params": {}}))
            with mock.patch.object(helper, "copy_chatdb") as copy_db, mock.patch.object(
                helper, "load_contacts"
            ) as load_contacts, mock.patch.object(helper, "write_response") as write_response:
                helper.process_request(request, [])

        copy_db.assert_not_called()
        load_contacts.assert_not_called()
        self.assertTrue(write_response.call_args.args[1]["ok"])

    def test_status_reports_protocol_version_and_runtime_checks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-status-test-") as td:
            chat_db = Path(td) / "chat.db"
            chat_db.write_bytes(b"fixture")
            with mock.patch.object(helper, "CHAT_DB_PATH", chat_db), mock.patch.object(
                helper, "CONFIRM_HELPER_PATH", Path(td) / "confirm-imessage-send"
            ):
                result = helper.action_status({}, None, {}, [])

        self.assertEqual(result["helper_version"], helper.HELPER_VERSION)
        self.assertEqual(result["protocol_version"], helper.PROTOCOL_VERSION)
        self.assertTrue(result["checks"]["chat_db_exists"])
        self.assertNotIn("text", json.dumps(result))


class DoctorTests(unittest.TestCase):
    def test_doctor_json_reports_actionable_failures(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-doctor-test-") as td:
            bridge = Path(td) / "bridge"
            result = subprocess.run(
                [
                    "python3",
                    str(REPO_ROOT / "tools" / "doctor.py"),
                    "--bridge",
                    str(bridge),
                    "--json",
                    "--skip-grok",
                    "--skip-launchd",
                    "--skip-codesign",
                    "--skip-chat-db",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertFalse(report["ok"])
        self.assertIn("bridge_root", report["checks"])
        self.assertEqual(report["checks"]["bridge_root"]["status"], "fail")

    def test_doctor_json_passes_for_synthetic_install(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-doctor-test-") as td:
            bridge = Path(td) / "bridge"
            for directory in (
                bridge / "bin",
                bridge / "control" / "requests",
                bridge / "control" / "responses",
                bridge / "contacts",
            ):
                directory.mkdir(parents=True, mode=0o700)
            bridge.chmod(0o700)
            for path in (
                bridge / "bin" / "helper.py",
                bridge / "bin" / "send_gate.py",
            ):
                path.write_text("# fixture\n")
                path.chmod(0o500)
            for path in (
                bridge / "bin" / "cowork-imessage-helper",
                bridge / "bin" / "confirm-imessage-send",
            ):
                path.write_text("fixture")
                path.chmod(0o700)
            blocklist = bridge / "contacts" / "blocked_chats.txt"
            blocklist.write_text("")
            blocklist.chmod(0o600)
            allowlist = bridge / "contacts" / "allowed_chats.txt"
            allowlist.write_text("")
            allowlist.chmod(0o600)
            read_policy = bridge / "contacts" / "read_policy.txt"
            read_policy.write_text("blocklist\n")
            read_policy.chmod(0o600)
            log = bridge / "control" / "log.txt"
            log.write_text("")
            log.chmod(0o600)

            result = subprocess.run(
                [
                    "python3",
                    str(REPO_ROOT / "tools" / "doctor.py"),
                    "--bridge",
                    str(bridge),
                    "--json",
                    "--skip-grok",
                    "--skip-launchd",
                    "--skip-codesign",
                    "--skip-chat-db",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])


class SkillInstallTests(unittest.TestCase):
    def test_skill_installer_uses_grok_discovery_path(self) -> None:
        script = (REPO_ROOT / "install-skill.sh").read_text()
        self.assertIn('GROK_SKILLS_ROOT="${GROK_HOME:-$HOME/.grok}/skills"', script)
        self.assertIn('SKILL_DEST="$GROK_SKILLS_ROOT/imessage-grok-bot"', script)
        self.assertIn("grok inspect", script)

    def test_skill_discovery_warning_is_nonfatal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-skill-test-") as td:
            root = Path(td)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            grok = fake_bin / "grok"
            grok.write_text("#!/bin/sh\necho 'no matching skills'\n")
            grok.chmod(0o755)
            env = os.environ.copy()
            env["GROK_HOME"] = str(root / "grok-home")
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

            result = subprocess.run(
                ["bash", str(REPO_ROOT / "install-skill.sh")],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("did not report imessage-grok-bot", result.stderr)
            self.assertTrue(
                (root / "grok-home" / "skills" / "imessage-grok-bot" / "SKILL.md").is_file()
            )

    def test_main_installer_supports_skipping_skill_copy(self) -> None:
        script = (REPO_ROOT / "install.sh").read_text()
        self.assertIn('INSTALL_GROK_SKILL="${INSTALL_GROK_SKILL:-1}"', script)
        self.assertIn('if [[ "$INSTALL_GROK_SKILL" == "1" ]]', script)
        self.assertIn("--skip-grok", script)

    def test_protocol_version_is_documented(self) -> None:
        protocol = (REPO_ROOT / "docs" / "PROTOCOL.md").read_text()
        self.assertIn(f"Protocol version: `{helper.PROTOCOL_VERSION}`", protocol)

    def test_release_version_matches_helper(self) -> None:
        result = subprocess.run(
            [
                "python3",
                str(REPO_ROOT / "tools" / "check_version.py"),
                f"v{helper.HELPER_VERSION}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
