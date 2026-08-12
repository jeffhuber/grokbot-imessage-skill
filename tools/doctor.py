#!/usr/bin/env python3
"""Diagnose a Grok Bot iMessage helper installation without reading messages."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
from typing import Any


def check(status: str, detail: str) -> dict[str, str]:
    return {"status": status, "detail": detail}


def mode(path: pathlib.Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def inspect_install(args: argparse.Namespace) -> dict[str, Any]:
    bridge = args.bridge.expanduser().resolve()
    checks: dict[str, dict[str, str]] = {}

    bridge_ok = bridge.is_dir() and mode(bridge) & 0o077 == 0
    checks["bridge_root"] = check(
        "pass" if bridge_ok else "fail",
        f"{bridge} mode={oct(mode(bridge)) if bridge.exists() else 'missing'}",
    )

    directory_modes = {
        "requests_dir": bridge / "control" / "requests",
        "responses_dir": bridge / "control" / "responses",
        "contacts_dir": bridge / "contacts",
    }
    for name, path in directory_modes.items():
        ok = path.is_dir() and mode(path) & 0o077 == 0
        checks[name] = check("pass" if ok else "fail", f"{path} mode={oct(mode(path)) if path.exists() else 'missing'}")

    executable_files = {
        "fda_wrapper": bridge / "bin" / "cowork-imessage-helper",
        "confirmation_helper": bridge / "bin" / "confirm-imessage-send",
    }
    for name, path in executable_files.items():
        ok = path.is_file() and os.access(path, os.X_OK) and mode(path) & 0o077 == 0
        checks[name] = check("pass" if ok else "fail", str(path))

    protected_files = {
        "helper_source": (bridge / "bin" / "helper.py", 0o500),
        "send_gate_source": (bridge / "bin" / "send_gate.py", 0o500),
        "blocklist": (bridge / "contacts" / "blocked_chats.txt", 0o600),
        "log": (bridge / "control" / "log.txt", 0o600),
    }
    for name, (path, expected) in protected_files.items():
        ok = path.is_file() and mode(path) == expected
        detail = f"{path} mode={oct(mode(path)) if path.exists() else 'missing'} expected={oct(expected)}"
        checks[name] = check("pass" if ok else "fail", detail)

    if not args.skip_codesign:
        for name, path in executable_files.items():
            result = run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(path)])
            checks[f"{name}_signature"] = check(
                "pass" if result.returncode == 0 else "fail",
                (result.stderr or result.stdout or "signature valid").strip(),
            )

    if not args.skip_launchd:
        launchctl = shutil.which("launchctl")
        if launchctl is None:
            checks["launchd"] = check("fail", "launchctl not found")
        else:
            uid = os.getuid()
            result = run([launchctl, "print", f"gui/{uid}/com.user.cowork-imessage"])
            checks["launchd"] = check(
                "pass" if result.returncode == 0 else "fail",
                "LaunchAgent loaded" if result.returncode == 0 else "LaunchAgent not loaded",
            )

    if not args.skip_chat_db:
        chat_db = pathlib.Path.home() / "Library" / "Messages" / "chat.db"
        ok = chat_db.is_file() and os.access(chat_db, os.R_OK)
        checks["chat_db"] = check(
            "pass" if ok else "fail",
            f"{chat_db} {'readable' if ok else 'missing or not readable; check Full Disk Access'}",
        )

    if not args.skip_grok:
        grok = shutil.which("grok")
        if grok is None:
            checks["grok_skill"] = check("fail", "grok CLI not found")
        else:
            result = run([grok, "inspect"])
            discovered = result.returncode == 0 and "imessage-grok-bot" in result.stdout
            checks["grok_skill"] = check(
                "pass" if discovered else "fail",
                "skill discovered" if discovered else "grok inspect did not report imessage-grok-bot",
            )

    return {
        "ok": all(value["status"] == "pass" for value in checks.values()),
        "bridge": str(bridge),
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", required=True, type=pathlib.Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--skip-grok", action="store_true")
    parser.add_argument("--skip-launchd", action="store_true")
    parser.add_argument("--skip-codesign", action="store_true")
    parser.add_argument("--skip-chat-db", action="store_true")
    args = parser.parse_args()

    report = inspect_install(args)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for name, result in report["checks"].items():
            print(f"[{result['status'].upper():4}] {name}: {result['detail']}")
        print("\nOverall:", "healthy" if report["ok"] else "attention required")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
