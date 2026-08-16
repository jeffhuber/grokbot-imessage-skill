"""`list_chats` (protocol 1.2) and the worker-side role gate.

The fixture database plants sentinel strings in every message body column
(`message.text` and `message.attributedBody`). The action must never select
those columns, so the assertions here check three things independently:

1. no SQL statement the action executes mentions either column,
2. no sentinel string appears anywhere in the serialized response, and
3. the role gate refuses `list_chats` on a host bridge and refuses
   body-returning actions on a manager bridge — in the worker, not a layer above.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests._helper_loader import helper

TEXT_SENTINEL = "SENTINEL-TEXT-BODY-9f1c"
ATTR_SENTINEL = b"SENTINEL-ATTRIBUTED-BODY-2b7e"

_ROLE_ENV = "IMESSAGE_BRIDGE_ROLE"


def _apple_ns(days_ago: float) -> int:
    return helper.to_apple_ns(time.time() - days_ago * 86400)


def build_fixture_db(path: Path) -> None:
    """A minimal chat.db shape: chat, handle, message and the join tables."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE chat (
                ROWID INTEGER PRIMARY KEY,
                chat_identifier TEXT,
                display_name TEXT,
                service_name TEXT,
                style INTEGER
            );
            CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                date INTEGER,
                text TEXT,
                attributedBody BLOB,
                is_from_me INTEGER,
                handle_id INTEGER
            );
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
            """
        )
        handles = {1: "+14155551234", 2: "+14155559876", 3: "bob@example.com", 4: "+14155550000"}
        for rowid, hid in handles.items():
            conn.execute("INSERT INTO handle VALUES (?, ?)", (rowid, hid))
        chats = [
            # (ROWID, chat_identifier, display_name, service, style)
            (1, "+14155551234", "", "iMessage", 45),
            (2, "chat100200300", "Family", "iMessage", 43),
            (3, "chat400500600", "", "iMessage", 43),   # unnamed group -> participant label
            (4, "+14155550000", "", "SMS", 45),          # old activity, outside 30d window
        ]
        for row in chats:
            conn.execute("INSERT INTO chat VALUES (?, ?, ?, ?, ?)", row)
        conn.executemany(
            "INSERT INTO chat_handle_join VALUES (?, ?)",
            [(1, 1), (2, 1), (2, 2), (2, 3), (3, 2), (3, 3), (4, 4)],
        )
        messages = [
            # (ROWID, days_ago, is_from_me, handle_id, chat_id)
            (1, 1, 0, 1, 1),
            (2, 0.5, 1, None, 1),
            (3, 3, 0, 2, 2),
            (4, 2, 0, 3, 2),
            (5, 10, 0, 2, 3),
            (6, 400, 0, 4, 4),
        ]
        for rowid, days_ago, is_me, handle_id, chat_id in messages:
            conn.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?, ?)",
                (
                    rowid,
                    _apple_ns(days_ago),
                    f"{TEXT_SENTINEL}-{rowid}",
                    ATTR_SENTINEL + str(rowid).encode(),
                    is_me,
                    handle_id,
                ),
            )
            conn.execute("INSERT INTO chat_message_join VALUES (?, ?)", (chat_id, rowid))
        conn.commit()
    finally:
        conn.close()


class _FixtureMixin:
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="imessage-list-chats-")
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "chat.db"
        build_fixture_db(self.db_path)
        self.conn = sqlite3.connect(str(self.db_path))
        self.addCleanup(self.conn.close)
        self.statements: list[str] = []
        self.conn.set_trace_callback(self.statements.append)
        self.contacts = {"4155551234": "Alice Example", "4155559876": "Carol Example"}
        self._old_role = os.environ.get(_ROLE_ENV)
        self.addCleanup(self._restore_role)

    def _restore_role(self) -> None:
        if self._old_role is None:
            os.environ.pop(_ROLE_ENV, None)
        else:
            os.environ[_ROLE_ENV] = self._old_role

    def _list(self, **params):
        return helper.action_list_chats(params, self.conn, self.contacts, [])


class ListChatsBodyBoundaryTests(_FixtureMixin, unittest.TestCase):
    def test_never_selects_body_columns(self) -> None:
        self._list()
        joined = "\n".join(self.statements).lower()
        self.assertTrue(self.statements, "expected the action to run SQL")
        self.assertNotIn("attributedbody", joined)
        self.assertNotIn("text", joined)

    def test_response_contains_no_sentinel_bodies(self) -> None:
        result = self._list(days=3650, limit=500)
        blob = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(TEXT_SENTINEL, blob)
        self.assertNotIn(ATTR_SENTINEL.decode(), blob)
        for item in result["chats"]:
            self.assertEqual(
                set(item),
                {
                    "chat_id", "kind", "display_name", "label", "participants",
                    "participant_count", "service", "message_count", "last_activity_date",
                },
            )


class ListChatsShapeTests(_FixtureMixin, unittest.TestCase):
    def test_default_window_and_ordering(self) -> None:
        result = self._list()
        self.assertEqual(result["window_days"], helper.LIST_CHATS_DEFAULT_DAYS)
        self.assertFalse(result["truncated"])
        ids = [c["chat_id"] for c in result["chats"]]
        # Most recent activity first; the 400-day-old SMS chat is outside 365d.
        self.assertEqual(ids, ["+14155551234", "chat100200300", "chat400500600"])
        self.assertEqual(result["chat_count"], 3)

    def test_item_fields(self) -> None:
        by_id = {c["chat_id"]: c for c in self._list()["chats"]}
        direct = by_id["+14155551234"]
        self.assertEqual(direct["kind"], "direct")
        self.assertEqual(direct["label"], "Alice Example")
        self.assertEqual(direct["participants"], ["+14155551234"])
        self.assertEqual(direct["participant_count"], 1)
        self.assertEqual(direct["service"], "iMessage")
        self.assertEqual(direct["message_count"], 2)
        self.assertRegex(direct["last_activity_date"], r"^\d{4}-\d{2}-\d{2}$")

        family = by_id["chat100200300"]
        self.assertEqual(family["kind"], "group")
        self.assertEqual(family["label"], "Family")
        self.assertEqual(family["participant_count"], 3)
        self.assertEqual(family["message_count"], 2)

        unnamed = by_id["chat400500600"]
        self.assertEqual(unnamed["kind"], "group")
        self.assertEqual(unnamed["display_name"], "")
        # Participant-derived label: first name for a known contact, raw email otherwise.
        self.assertEqual(unnamed["label"], "Carol, bob@example.com")

    def test_wide_window_includes_old_chat(self) -> None:
        ids = [c["chat_id"] for c in self._list(days=3650)["chats"]]
        self.assertIn("+14155550000", ids)
        self.assertEqual(ids[-1], "+14155550000")

    def test_limit_and_truncated(self) -> None:
        result = self._list(limit=2)
        self.assertEqual(result["chat_count"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual([c["chat_id"] for c in result["chats"]], ["+14155551234", "chat100200300"])

    def test_include_groups_false(self) -> None:
        result = self._list(include_groups=False, days=3650)
        self.assertEqual({c["kind"] for c in result["chats"]}, {"direct"})
        self.assertEqual(result["chat_count"], 2)

    def test_query_matches_label_and_handles(self) -> None:
        self.assertEqual(
            [c["chat_id"] for c in self._list(query="alice")["chats"]], ["+14155551234"]
        )
        self.assertEqual(
            [c["chat_id"] for c in self._list(query="bob@")["chats"]],
            ["chat100200300", "chat400500600"],
        )
        self.assertEqual(
            [c["chat_id"] for c in self._list(query="FAMILY")["chats"]], ["chat100200300"]
        )
        self.assertEqual(self._list(query="nobody-here")["chats"], [])

    def test_query_with_limit_reports_truncation(self) -> None:
        result = self._list(query="bob@", limit=1)
        self.assertEqual(result["chat_count"], 1)
        self.assertTrue(result["truncated"])


class ListChatsBoundsTests(_FixtureMixin, unittest.TestCase):
    def test_days_bounds(self) -> None:
        for bad in (0, -1, helper.MAX_LIST_CHATS_DAYS + 1, "x", True, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self._list(days=bad)
        self.assertEqual(self._list(days=helper.MAX_LIST_CHATS_DAYS)["window_days"],
                         helper.MAX_LIST_CHATS_DAYS)

    def test_limit_bounds(self) -> None:
        for bad in (0, -5, helper.MAX_LIMIT + 1, "many", 1.9, 2.5, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self._list(limit=bad)
        # integral floats and numeric strings are accepted as integers
        self.assertEqual(self._list(limit=2.0)["chat_count"], 2)
        self.assertEqual(self._list(limit="2")["chat_count"], 2)

    def test_participants_loaded_only_for_candidate_chats(self) -> None:
        # Add a chat with no messages: it must not be scanned by list_chats.
        self.conn.execute("INSERT INTO chat VALUES (99, 'chat999', 'Idle', 'iMessage', 43)")
        self.conn.execute("INSERT INTO chat_handle_join VALUES (99, 4)")
        self.conn.commit()
        self.statements.clear()
        self._list(days=30)
        joined = "\n".join(self.statements)
        self.assertTrue(any("chat_handle_join" in s and "WHERE c.ROWID IN" in s for s in self.statements), joined)
        self.assertNotIn("'chat999'", json.dumps(self._list(days=30)))

    def test_query_bounds(self) -> None:
        with self.assertRaises(ValueError):
            self._list(query="q" * (helper.MAX_LIST_CHATS_QUERY_LEN + 1))
        with self.assertRaises(ValueError):
            self._list(query=123)
        # Empty/whitespace query means "no filter".
        self.assertEqual(self._list(query="   ")["chat_count"], 3)

    def test_include_groups_must_be_boolean(self) -> None:
        for bad in ("yes", 1, 0, "false"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self._list(include_groups=bad)

    def test_distinct_from_review_bounds(self) -> None:
        self.assertGreater(helper.MAX_LIST_CHATS_DAYS, helper.MAX_DAYS)
        with self.assertRaises(ValueError):
            helper.validate_days(helper.MAX_DAYS + 1)


class RoleGateTests(_FixtureMixin, unittest.TestCase):
    """The worker enforces the role table from PROTOCOL.md."""

    def setUp(self) -> None:
        super().setUp()
        self._bridge_dir = tempfile.TemporaryDirectory(prefix="imessage-role-bridge-")
        self.addCleanup(self._bridge_dir.cleanup)
        root = Path(os.path.realpath(self._bridge_dir.name))
        root.chmod(0o700)
        control = root / "control"
        self.requests_dir = control / "requests"
        self.responses_dir = control / "responses"
        for d in (self.requests_dir, self.responses_dir):
            d.mkdir(parents=True, mode=0o700)
        control.chmod(0o700)
        self._patches = [
            mock.patch.object(helper, "BRIDGE_ROOT", root),
            mock.patch.object(helper, "REQUESTS_DIR", self.requests_dir),
            mock.patch.object(helper, "RESPONSES_DIR", self.responses_dir),
            mock.patch.object(helper, "LOG_PATH", root / "control" / "log.txt"),
            mock.patch.object(helper, "copy_chatdb", return_value=self.db_path),
            mock.patch.object(helper, "cleanup_tmpdb", lambda p: None),
            mock.patch.object(helper, "load_contacts", return_value=self.contacts),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _run(self, action: str, params: dict | None = None) -> dict:
        stem = f"t-{action}"
        req = self.requests_dir / f"request-{stem}.json"
        req.write_text(json.dumps({"id": stem, "action": action, "params": params or {}}))
        helper.process_request(req, [])
        return json.loads((self.responses_dir / f"response-{stem}.json").read_text())

    def test_default_role_is_host(self) -> None:
        os.environ.pop(_ROLE_ENV, None)
        self.assertEqual(helper.bridge_role(), "host")
        self.assertEqual(set(helper.allowed_actions()), set(helper._HOST_ACTIONS))
        self.assertNotIn("list_chats", helper.allowed_actions())

    def test_role_table_covers_every_action_exactly(self) -> None:
        self.assertEqual(
            set(helper.ACTIONS),
            set(helper._HOST_ACTIONS) | set(helper._MANAGER_ACTIONS),
        )
        # Body-returning and send actions are never on the manager bridge.
        for body_action in ("review", "search", "chat_history", "response_stats",
                            "send_preview", "send"):
            self.assertNotIn(body_action, helper._MANAGER_ACTIONS)
        self.assertEqual(set(helper._MANAGER_ACTIONS), {"status", "contacts_lookup", "list_chats"})

    def test_host_bridge_refuses_list_chats(self) -> None:
        os.environ.pop(_ROLE_ENV, None)
        resp = self._run("list_chats")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "action not permitted on this bridge")
        self.assertEqual(resp["bridge_role"], "host")
        self.assertEqual(resp["allowed_actions"], sorted(helper._HOST_ACTIONS))
        self.assertNotIn("chats", resp)

    def test_manager_bridge_serves_list_chats(self) -> None:
        os.environ[_ROLE_ENV] = "manager"
        resp = self._run("list_chats", {"days": 30})
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["action"], "list_chats")
        self.assertEqual(resp["chat_count"], 3)
        self.assertNotIn(TEXT_SENTINEL, json.dumps(resp))

    def test_bridge_role_is_case_insensitive(self) -> None:
        os.environ[_ROLE_ENV] = "Manager"
        self.assertEqual(helper.bridge_role(), "manager")
        self.assertEqual(helper.allowed_actions(), helper._MANAGER_ACTIONS)

    def test_manager_bridge_refuses_body_actions(self) -> None:
        os.environ[_ROLE_ENV] = "manager"
        for action in ("review", "search", "chat_history", "response_stats", "send_preview", "send"):
            with self.subTest(action=action):
                resp = self._run(action, {"days": 1, "term": "x", "chat": "x", "to": "+15555550100", "text": "hi"})
                self.assertFalse(resp["ok"])
                self.assertEqual(resp["error"], "action not permitted on this bridge")
                self.assertEqual(resp["allowed_actions"], sorted(helper._MANAGER_ACTIONS))

    def test_manager_status_reports_role(self) -> None:
        os.environ[_ROLE_ENV] = "manager"
        resp = self._run("status")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["protocol_version"], "1.2")
        self.assertEqual(resp["bridge_role"], "manager")
        self.assertEqual(resp["allowed_actions"], sorted(helper._MANAGER_ACTIONS))

    def test_unknown_role_fails_closed(self) -> None:
        os.environ[_ROLE_ENV] = "admin"
        self.assertEqual(helper.allowed_actions(), ())
        resp = self._run("status")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "action not permitted on this bridge")
        self.assertEqual(resp["allowed_actions"], [])

    def test_execution_error_carries_allowed_actions(self) -> None:
        os.environ[_ROLE_ENV] = "manager"
        resp = self._run("list_chats", {"days": 0})
        self.assertFalse(resp["ok"])
        self.assertIn("days must be", resp["error"])
        self.assertEqual(resp["allowed_actions"], sorted(helper._MANAGER_ACTIONS))

    def test_unknown_action_lists_role_actions(self) -> None:
        os.environ.pop(_ROLE_ENV, None)
        resp = self._run("dump_everything")
        self.assertFalse(resp["ok"])
        self.assertIn("unknown action", resp["error"])
        self.assertEqual(resp["allowed_actions"], sorted(helper._HOST_ACTIONS))


if __name__ == "__main__":
    unittest.main()
