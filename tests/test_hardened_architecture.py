from __future__ import annotations

import os
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._helper_loader import REPO_ROOT, helper


ALLOWLIST_TOOL_PATH = REPO_ROOT / "tools" / "configure_allowlist.py"
ALLOWLIST_SPEC = importlib.util.spec_from_file_location(
    "grokbot_configure_allowlist", ALLOWLIST_TOOL_PATH
)
if ALLOWLIST_SPEC is None or ALLOWLIST_SPEC.loader is None:
    raise RuntimeError(f"could not load {ALLOWLIST_TOOL_PATH}")
configure_allowlist = importlib.util.module_from_spec(ALLOWLIST_SPEC)
ALLOWLIST_SPEC.loader.exec_module(configure_allowlist)


class ReadPolicyTests(unittest.TestCase):
    def test_allowlist_defaults_to_deny_and_blocklist_takes_precedence(self) -> None:
        policy = helper.PrivacyPolicy(
            mode="allowlist",
            allowlist=("+14155551234", "friend@example.com"),
            blocklist=("friend@example.com",),
        )
        messages = [
            {"chat_id": "+14155551234", "sender": "+14155551234"},
            {"chat_id": "friend@example.com", "sender": "friend@example.com"},
            {"chat_id": "+14155559876", "sender": "+14155559876"},
        ]

        filtered = helper.apply_read_policy(messages, policy)

        self.assertEqual(filtered, messages[:1])

    def test_allowlist_applies_to_contact_lookup(self) -> None:
        policy = helper.PrivacyPolicy(
            mode="allowlist",
            allowlist=("alice@example.com",),
            blocklist=(),
        )
        result = helper.action_contacts_lookup(
            {"name": "Example"},
            None,
            {"alice@example.com": "Alice Example", "bob@example.com": "Bob Example"},
            policy,
        )
        self.assertEqual(result["matches"], [{"name": "Alice Example", "email": "alice@example.com"}])

    def test_email_entries_match_exactly(self) -> None:
        policy = helper.PrivacyPolicy(
            mode="allowlist", allowlist=("alice@example.com",), blocklist=()
        )
        self.assertTrue(helper.is_read_allowed("alice@example.com", "", policy))
        self.assertFalse(helper.is_read_allowed("alice@example.com.evil", "", policy))

    def test_disallowed_contact_metadata_is_not_resolved(self) -> None:
        policy = helper.PrivacyPolicy(mode="allowlist", allowlist=(), blocklist=())
        contacts = {"alice@example.com": "Alice Example"}
        self.assertEqual(helper.filter_contacts(contacts, policy), {})

        with mock.patch.object(helper, "mint_send_nonce", return_value="nonce"):
            preview = helper.action_send_preview(
                {"to": "alice@example.com", "text": "hello"},
                None,
                contacts,
                policy,
            )
        self.assertEqual(preview["preview"]["resolved_name"], "")

    def test_root_policy_requirement_rejects_user_owned_allowlist(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-policy-test-") as td:
            allowlist = Path(td) / "allowed.txt"
            allowlist.write_text("+14155551234\n")
            with mock.patch.dict(
                os.environ,
                {
                    "COWORK_IMESSAGE_READ_POLICY": "allowlist",
                    "COWORK_IMESSAGE_REQUIRE_ROOT_POLICY": "1",
                },
            ), mock.patch.object(helper, "ALLOWLIST_PATH", allowlist), mock.patch.object(
                helper, "BLOCKLIST_PATH", Path(td) / "missing-blocklist"
            ), mock.patch.object(helper, "log"):
                policy = helper.load_privacy_policy()

        self.assertEqual(policy.mode, "allowlist")
        self.assertEqual(policy.allowlist, ())
        self.assertFalse(helper.is_read_allowed("+14155551234", "+14155551234", policy))


@unittest.skipUnless(shutil.which("clang"), "clang is required")
class WrapperValidationTests(unittest.TestCase):
    def test_wrapper_validates_every_loaded_component(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grokbot-wrapper-test-") as td:
            root = Path(td)
            helper_script = root / "helper.py"
            send_gate = root / "send_gate.py"
            confirmation = root / "confirm"
            wrapper = root / "wrapper"
            helper_script.write_text(
                "import os; print(os.environ['COWORK_IMESSAGE_BRIDGE_DIR'])\n"
            )
            send_gate.write_text("# trusted fixture\n")
            confirmation.write_text("#!/bin/sh\nexit 0\n")
            helper_script.chmod(0o500)
            send_gate.chmod(0o500)
            confirmation.chmod(0o700)

            compile_result = subprocess.run(
                [
                    "clang",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-O2",
                    f'-DHELPER_SCRIPT="{helper_script}"',
                    f'-DSEND_GATE_SCRIPT="{send_gate}"',
                    f'-DCONFIRM_HELPER="{confirmation}"',
                    f'-DBRIDGE_ROOT="{root / "bridge"}"',
                    f'-DPYTHON_INTERPRETER="{sys.executable}"',
                    "-o",
                    str(wrapper),
                    str(REPO_ROOT / "bin" / "cowork_imessage_helper.c"),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)

            healthy = subprocess.run([str(wrapper)], capture_output=True, text=True, check=False)
            self.assertEqual(healthy.returncode, 0, healthy.stderr)
            self.assertEqual(healthy.stdout.strip(), str(root / "bridge"))

            send_gate.chmod(0o520)
            writable = subprocess.run([str(wrapper)], capture_output=True, text=True, check=False)
            self.assertEqual(writable.returncode, 5)
            self.assertIn("group/world writable", writable.stderr)

            send_gate.unlink()
            send_gate.symlink_to(helper_script)
            symlinked = subprocess.run([str(wrapper)], capture_output=True, text=True, check=False)
            self.assertEqual(symlinked.returncode, 3)
            self.assertIn("not a regular file", symlinked.stderr)


class HardenedInstallerTests(unittest.TestCase):
    def test_installer_bakes_root_owned_code_and_allowlist_requirements(self) -> None:
        script = (REPO_ROOT / "install-hardened.sh").read_text()
        self.assertIn('-DEXPECTED_CODE_UID=0', script)
        self.assertIn('-DREQUIRE_ROOT_POLICY=1', script)
        self.assertIn("-o root -g wheel", script)
        self.assertIn('READ_POLICY_MODE=\'"allowlist"\'', script)
        self.assertIn('USER_ROOT="$PRODUCT_ROOT/users/$UID"', script)

    def test_runtime_privacy_files_are_gitignored(self) -> None:
        entries = (REPO_ROOT / ".gitignore").read_text().splitlines()
        self.assertIn("contacts/allowed_chats.txt", entries)
        self.assertIn("contacts/read_policy.txt", entries)

    def test_allowlist_tool_has_fixed_per_user_destination_and_validation(self) -> None:
        path = configure_allowlist.allowlist_path()
        self.assertEqual(path.name, "allowed_chats.txt")
        self.assertEqual(path.parent.parent.name, str(os.getuid()))
        self.assertEqual(
            configure_allowlist.validate_entry("alice@example.com"),
            "alice@example.com",
        )
        for value in ("Alice Example", "bad@address", "chat id", "1234", "x\nroot"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                configure_allowlist.validate_entry(value)

    def test_plist_separates_code_and_runtime_roots(self) -> None:
        template = (REPO_ROOT / "com.user.cowork-imessage.plist.template").read_text()
        self.assertIn("{{CODE_ROOT}}/bin/cowork-imessage-helper", template)
        self.assertIn("{{BRIDGE_ROOT}}/control/requests", template)
        self.assertNotIn("{{INSTALL_ROOT}}", template)


if __name__ == "__main__":
    unittest.main()
