# Grok Bot iMessage Skill for macOS

> **Read, search, triage, and send iMessages** from Grok Bot via a local macOS helper bridge.

This repository provides a **Grok Bot skill** that lets Grok Bot interact with your iMessages on macOS. A local launchd helper reads your Messages database and sends via AppleScript. The helper makes no network requests, but message content selected for Grok is processed through xAI's normal service pipeline.

---

## What This Does

- **Read & Triage:** "Review my messages from the last 2 days" → Grok Bot reads your iMessages, categorizes threads by urgency, and surfaces what needs a reply.
- **Search:** "Find all messages mentioning 'dinner plans' in the last month" → Full-text search across your message history.
- **Chat History:** "Pull my conversation with Alex from the last week" → Retrieves a specific thread's recent messages.
- **Response Stats:** "What's my average reply time to Angel over the last 24 hours?" → Computes timing statistics.
- **Send (with preview-and-confirm):** "Text +1-555-123-4567: 'Running 10 minutes late'" → Grok Bot previews the message, you approve, then it sends via AppleScript.

---

## Requirements

- **macOS 13 or newer**
- **Xcode Command Line Tools** (for building the helper wrapper)
- **Python 3.9 or newer** (`/usr/bin/python3` from Xcode Command Line Tools works)
- **Grok Build CLI** for automatic skill discovery, or another Grok Bot host
  capable of writing the documented request files
- **Full Disk Access** permission (to read `~/Library/Messages/chat.db`)
- **Automation → Messages** permission (to send messages via AppleScript)

### Compatibility

| Component | Supported and verified | Notes |
|-----------|------------------------|-------|
| macOS | 13+; CI on `macos-latest`; manual check on macOS 26.5, Apple silicon | Messages database and AppleScript are private/legacy integration surfaces and may change in future macOS releases. |
| Python | 3.9, 3.11, and 3.13 in CI | The installer requires 3.9+. |
| CPU | Apple silicon | The installer compiles from source locally; Intel is expected to build but is not currently exercised in CI. |
| Grok | Grok Build user skills with `grok inspect` | The helper protocol is independently versioned and reports `1.1`; other hosts can use the bridge directly. |

---

## Installation (5–10 minutes)

### 1. Clone this repository

```bash
git clone https://github.com/jeffhuber/grokbot-imessage-skill.git
cd grokbot-imessage-skill
```

### 2. Choose an installation mode

**Hardened install (recommended):**

```bash
./install-hardened.sh
```

This invokes `sudo` only to install trusted code under a root-owned per-user path,
`/Library/Application Support/GrokBotIMessage/users/<uid>/libexec`. Requests, responses,
logs, and nonces remain in your user-owned
`~/Library/Application Support/GrokBotIMessage` bridge. Reads default to deny;
allow each phone, email, or group identifier explicitly:

```bash
CODE_ROOT="/Library/Application Support/GrokBotIMessage/users/$UID/libexec"
python3 "$CODE_ROOT/tools/configure_allowlist.py" \
  add +15551234567
```

The root-owned wrapper validates `helper.py`, `send_gate.py`, the confirmation
binary, and the root-owned allowlist before inheriting Full Disk Access.

**Standard per-user install:**

Run `./install.sh` to use the repository itself as both code and bridge, or copy
it to a dedicated folder first:

```bash
mkdir ~/imessage-bridge
cp -r bin/ tools/ contacts/ SKILL.md install.sh install-hardened.sh \
  install-skill.sh uninstall.sh uninstall-hardened.sh \
  com.jeffhuber.grokbot-imessage.plist.template ~/imessage-bridge/
cd ~/imessage-bridge && ./install.sh
```

The standard installer does not require `sudo`, but its Python code is writable
by processes running as your user. Its default `blocklist` policy is intended
to prevent accidental disclosure, not resist a compromised same-user process.

Both installers:
- Compile the C wrapper with your install path baked in
- Compile a native, scrollable send-confirmation window
- Code-sign locally (ad hoc unless `CODESIGN_IDENTITY` is supplied to the hardened installer)
- Set up the launchd agent to watch for request files
- Create `control/requests/`, `control/responses/`, and `contacts/` directories
- Install the skill under `${GROK_HOME:-~/.grok}/skills/imessage-grok-bot/`
- Verify discovery with `grok inspect` when the Grok CLI is available

The helper uses the host-specific LaunchAgent
`com.jeffhuber.grokbot-imessage`, so it can run beside the sibling Claude
Cowork helper. Each host must use its own bridge, policies, logs, responses,
and nonces; do not point both hosts at one queue.

That path is Grok Build's documented user-level skill directory. If your Grok
host already loads `SKILL.md` through a workflow bridge, install only the local
helper and skip the copy:

```bash
INSTALL_GROK_SKILL=0 ./install.sh
```

You can run `./install-skill.sh` later. See xAI's
[skills documentation](https://docs.x.ai/build/features/skills-plugins-marketplaces)
for the currently supported discovery paths.

### 3. Grant Full Disk Access

Open **System Settings → Privacy & Security → Full Disk Access**, click the `+` button, press **Cmd-Shift-G**, and paste the path printed by the installer:

Use the exact wrapper path printed by your installer. For hardened installs it is
`/Library/Application Support/GrokBotIMessage/users/<uid>/libexec/bin/grokbot-imessage-helper`;
for standard installs it is `<bridge>/bin/grokbot-imessage-helper`.

Select it, make sure the toggle is **ON**.

### 4. (Optional) Grant Automation permission for sending

The first time you ask Grok Bot to send a message, macOS will prompt: *"grokbot-imessage-helper wants to control Messages."* Click **OK**.

You can verify the grant later under **System Settings → Privacy & Security → Automation → grokbot-imessage-helper → Messages**.

### 5. Tell Grok Bot where your bridge folder is

When you first use an iMessage command, Grok Bot will ask: *"Where did you install the iMessage helper?"*

Provide the full bridge path printed by the installer. The hardened default is
`~/Library/Application Support/GrokBotIMessage`; a standard install uses the
folder where you ran `install.sh`.

---

## Usage

Once installed, ask Grok Bot things like:

- **Triage:** "Review my iMessages from the last day."
- **Search:** "Find messages about 'project deadline' in the last 2 weeks."
- **Chat History:** "Show my conversation with Angel from the last 3 days."
- **Response Time:** "How fast do I reply to Alex on average?"
- **Send (preview-first):** "Text +1-555-123-4567: 'On my way!'"

Grok Build uses the skill automatically when you ask iMessage-related questions.
Run `grok inspect` to confirm `imessage-grok-bot` appears in the discovered
skills. Skip this check when another host injects `SKILL.md` directly.

---

## Privacy & Security

### What This Helper Can Do

- **Read your entire Messages database** (`~/Library/Messages/chat.db`) — Full Disk Access is the coarsest macOS permission; the code only reads `chat.db`, but a bug or compromise becomes a full-user-file-read primitive.
- **Send iMessages/SMS** on your behalf via AppleScript (only after you approve the preview).

### What Leaves Your Mac

- **Message content** passes through Grok Bot's normal pipeline, which means it reaches xAI's servers as part of the conversation.
- **The helper itself does not make any outbound network connections.** Extraction, filtering, and redaction happen on-device; Grok processing happens through xAI's service.

### Third-Party Privacy

**Messages are two-sided.** Every message this helper reads was sent to or from someone else, and they never consented to have their words processed by an LLM. If you use this skill, you're making that choice on their behalf. This is an intrinsic property of giving an AI assistant access to your messages—mentioned here because it's a legitimate concern the README shouldn't bury.

### Read Policy

The recommended hardened install enforces a root-owned, default-deny allowlist.
Only listed phone numbers, emails, and group IDs can appear in message or contact
responses. Manage it with `configure_allowlist.py`; a same-user process cannot
broaden it without administrator approval.

The standard install uses `contacts/read_policy.txt` (`blocklist` by default).
Add sensitive threads to `contacts/blocked_chats.txt`. Blocked threads are
**dropped before response JSON is written**. Set `read_policy.txt` to `allowlist`
to use the user-editable `contacts/allowed_chats.txt` instead.

Format:
```
# One entry per line. Lines starting with # are ignored.
+15551234567
lawyer@example.com
chat123456789
```

Phone numbers match by last 10 digits. Emails and group IDs match case-insensitively.
The blocklist always takes precedence, including for sends.

### Send Gate (Preview-and-Confirm)

Sending is gated at the **helper level** with two layers of protection:

**1. Nonce validation (v0.4.0+):**
   - Grok Bot issues a `send_preview` → the helper returns the normalized payload and a **single-use send nonce** bound to that exact `(to, text, service)` triple.
   - Grok Bot shows you the preview in chat; you approve.
   - Grok Bot issues a `send` with the nonce → the helper verifies the nonce matches the payload.

**2. Native macOS dialog (v1.0.0+):**
   - After nonce validation succeeds, the helper displays a **native macOS system dialog** showing:
     - Recipient name and exact phone/email address
     - Service (iMessage or SMS)
     - Full message text in a scrollable, read-only view
   - **Cancel is the keyboard default.** You must deliberately select **Send** to proceed. Clicking Cancel or waiting 60 seconds aborts the send.
   - This dialog enforces human approval at the macOS level—even a valid nonce requires explicit user confirmation.

Nonces expire after 60 seconds, are single-use, and are deleted on any validation failure. A process that can write to the bridge folder cannot silently send without both: (a) racing a real user-approved preview inside its 60s window, and (b) the user clicking **Send** in the native dialog that appears on their screen.

See **[SECURITY.md](./SECURITY.md)** for full threat-model details.

---

## Architecture

```
  Grok Bot (your context)             macOS (your local machine)
  -----------------------             -------------------------
  Writes request-<id>.json  -->  launchd watches control/requests/  -->
  Reads  response-<id>.json  <-- grokbot-imessage-helper (wrapper)   -->
                                 root-owned helper.py in hardened mode
                                 (FDA-granted, reads chat.db)
                                 grokbot-imessage-confirm (native approval UI)
```

Grok Bot runs in an environment that can execute shell commands on your Mac. The
helper is a LaunchAgent that watches a user-owned **bridge folder**. In hardened
mode, executable code is a separate root-owned tree; only queues and runtime
state are writable by the user or host.

Sending uses the **same** request/response bridge. Grok Bot writes a `send_preview` or `send` request, the helper calls `osascript` to drive Messages.app via AppleScript, and the result comes back as JSON. No GUI scripting or automated clicks; sends require a native confirmation dialog click—just a short-lived subprocess plus human approval.

---

## Protocol

Full JSON protocol documentation: **[docs/PROTOCOL.md](./docs/PROTOCOL.md)**

Quick reference:

| Action | What It Does |
|--------|--------------|
| `review` | Triage recent messages into needs-reply / low-priority / skip buckets |
| `search` | Full-text substring search across all threads (sorted newest first) |
| `chat_history` | Recent messages in one thread (by name, phone, email, or group ID—**group IDs NOT supported for sending**) |
| `response_stats` | Avg/median/min/max reply times to one contact |
| `contacts_lookup` | Find matching contacts by name |
| `send_preview` | Dry-run validation (returns a single-use nonce) |
| `send` | Actually send (requires the nonce from `send_preview` + native macOS dialog confirmation) |
| `status` | Report helper/protocol versions and installation checks without reading messages |

---

## Smoke Test

After installation, verify everything works: **[docs/SMOKE_TEST.md](./docs/SMOKE_TEST.md)**

Quick sanity check:

```bash
BRIDGE="/path/to/your/bridge-folder"  # e.g., ~/imessage-bridge
REQ_ID=$(date +%s)

TMP="$BRIDGE/control/requests/.request-$REQ_ID.json.tmp"
FINAL="$BRIDGE/control/requests/request-$REQ_ID.json"

cat > "$TMP" <<EOF
{"id": "$REQ_ID", "action": "contacts_lookup", "params": {"name": "test"}}
EOF
mv "$TMP" "$FINAL"

# Poll for response (should appear within 2-5 seconds)
for i in {1..20}; do
  if [[ -f "$BRIDGE/control/responses/response-$REQ_ID.json" ]]; then
    RESPONSE="$BRIDGE/control/responses/response-$REQ_ID.json"
    cat "$RESPONSE"
    rm -f "$RESPONSE"  # Responses contain message data; delete after parsing.
    break
  fi
  sleep 0.5
done
```

If a response appears with `"ok": true` or `"ok": false`, the helper is working. Check `$BRIDGE/control/log.txt` for errors if not.

Response files are mode `600`. Clients must delete them immediately after parsing; the helper also removes abandoned responses after one hour. Logs never include message bodies or raw `attributedBody` bytes and rotate at 1 MiB with three backups.

The helper opens the bridge and its runtime directories through anchored,
no-follow directory descriptors. It rejects symlinks, non-user-owned objects,
and group/world-accessible runtime directories instead of changing their
permissions. Request files must be regular files owned by the current user and
are limited to 64 KiB; nonce files must also be regular, current-user-owned,
and mode `600`. Rerun the installer if `doctor.py` reports unsafe modes.

Message reads use SQLite's [online backup API](https://sqlite.org/backup.html)
to create a consistent temporary snapshot, including committed rows still in
Messages' live WAL. The source is opened read-only without `immutable=1`:
SQLite [defines that flag](https://sqlite.org/uri.html#uriimmutable) as an
assertion that a database cannot change, which is not true while Messages is
running.

---

## Limitations

- **Text only.** No attachments, images, stickers, audio, Tapback reactions, message effects, or message editing/deletion.
- **No group-chat sending.** Can read from group chats (they appear in `review` and `chat_history`), but cannot send *to* group chat IDs. Use individual phone numbers or emails for sending.
- **No group-chat creation.** Cannot create new group chats.
- **Local `chat.db` only.** If a thread hasn't synced to this Mac, it won't appear.
- **macOS-specific.** Relies on direct `chat.db` access and AppleScript control of Messages.app—both Apple surfaces that could be deprecated in a future macOS release.

---

## Troubleshooting

Run the non-destructive setup diagnostic first. Add `--skip-grok` when you
intentionally skipped the Grok Build skill copy:

```bash
python3 tools/doctor.py --bridge "/path/to/your/bridge-folder"
```

| Symptom | Cause | Fix |
|---------|-------|-----|
| No response files appear | FDA not granted | Grant FDA to the exact wrapper path printed by the installer |
| `sqlite3.OperationalError` in logs | FDA not granted or stale | Re-add the wrapper in System Settings → Full Disk Access |
| Send fails on first attempt | Automation permission needed | Click **OK** on the macOS prompt; future sends will work |
| `send gate: missing nonce` | Skill didn't call `send_preview` first | Report a bug—the skill should always preview before send |
| Messages decode as empty | `attributedBody` parser failed | Check `control/log.txt` for unparseable blobs |
| `UnsafeRuntimePath` or `unsafe bridge root` | A bridge path is a symlink, has the wrong owner, or is not private | Remove the unsafe object or rerun the installer; do not chmod an unexplained target |

Check `<bridge>/control/log.txt` first when debugging.

---

## Uninstalling

Use `./uninstall-hardened.sh` for a hardened install or `./uninstall.sh` for a
standard install. Both preserve runtime data until you deliberately delete it.

This removes the launchd agent. To fully remove:
- Delete the bridge folder.
- Open **System Settings → Privacy & Security → Full Disk Access** and revoke `grokbot-imessage-helper`.
- (Optional) Open **System Settings → Privacy & Security → Automation** and toggle off or remove `grokbot-imessage-helper → Messages`.

The uninstaller also removes the user-level `imessage-grok-bot` skill installed under Grok's discovery directory.

## Upgrading

Pull or unpack the new source, review `CHANGELOG.md`, then rerun the same installer
you originally chose. The installer rebuilds and re-signs the native binaries,
restarts the LaunchAgent, and refreshes the discovered skill. Recheck Full Disk
Access if macOS no longer recognizes the rebuilt wrapper, then run:

```bash
python3 tools/doctor.py --bridge "$PWD"
```

For hardened installs, use the full command printed by `install-hardened.sh`,
including `--code-root`.

### Migration from the legacy shared LaunchAgent

Older releases used `com.user.cowork-imessage`, the same identity as the
original Claude Cowork helper. During upgrade, the installer removes that
legacy agent only when its plist points to the exact Grok code and bridge paths
being upgraded. A legacy agent belonging to Claude or an unknown installation
is left untouched. The new wrapper name may require granting Full Disk Access
and Automation once more; follow the exact paths printed by the installer.

Tagged releases include `SHA256SUMS`; verify an archive with
`shasum -a 256 -c SHA256SUMS` before installation.
Release archives are source-only and are not notarized; see
[`docs/SIGNING.md`](./docs/SIGNING.md) for the exact trust model.

---

## Related Projects

**Looking for Claude Cowork?** The sibling integration for Claude Cowork lives at [github.com/jeffhuber/claudecowork-imessage-skill](https://github.com/jeffhuber/claudecowork-imessage-skill). Same helper, different host adapter.

---

## Contributing

PRs welcome! If you find a bug or want to add a feature:

1. Open an issue first to discuss the change.
2. Submit a PR with tests under `tests/`.
3. Follow the existing code style (Python 3.9+, type hints where helpful).
4. Run `python3 -m unittest discover -s tests -v`, `bash -n` on the shell
   scripts, and the native compile checks from `.github/workflows/ci.yml`.

**Security issues:** Email <jhuber+grokbotimessage@gmail.com> instead of opening a public issue. See **[SECURITY.md](./SECURITY.md)** for details.

---

## License

MIT. See **[LICENSE](./LICENSE)** for full text.

---

## Acknowledgments

- Protocol design and helper implementation adapted from the [claudecowork-imessage-skill](https://github.com/jeffhuber/claudecowork-imessage-skill) project.
- Inspired by the need for local, privacy-first AI assistant integrations on macOS.
