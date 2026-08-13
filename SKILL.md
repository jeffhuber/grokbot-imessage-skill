---
name: imessage-grok-bot
description: >
  Read, search, triage, and send iMessages on macOS via the iMessage helper
  bridge. Use when the user asks to review messages, find messages that need
  a reply, search message history, pull a conversation, compute response-time
  stats, or send a plain-text iMessage. macOS only—uses an on-device launchd
  helper to query the Messages SQLite database and AppleScript (osascript) to
  send outbound messages.
version: 1.2.0
---

# iMessage on macOS — Grok Bot

## When to use this skill

Use when the user asks to:

- Review / triage recent iMessages or SMS
- Find messages that need a response
- Search history for a topic, person, or phrase
- Pull a specific conversation's recent messages
- Compute response-time statistics (e.g., "average reply time to Angel over the last 24 hours")
- Send a plain-text iMessage to an existing contact

## Architecture

```
  Grok Bot (your context)             macOS (user's local machine)
  -----------------------             ---------------------------
  Writes request-<id>.json  -->  launchd watches control/requests/  -->
  Reads  response-<id>.json  <-- grokbot-imessage-helper (wrapper)   -->
                                 helper.py (FDA-granted, reads
                                 chat.db + AddressBook, drives osascript)
```

You run in an environment that can execute shell commands on the user's Mac. The iMessage helper is a launchd agent that watches a **bridge folder** for JSON request files. When you write a request, launchd fires the helper, which reads the Messages database, processes the request, and writes a JSON response back—where you can then read it.

Sending goes through the **same** request/response bridge. You write a `send_preview` or `send` request, the helper calls `osascript` to drive Messages.app via AppleScript, and the result comes back as JSON. No GUI scripting or automated clicks; sends require a native confirmation dialog click—just a short-lived subprocess plus human approval.

---

## Prerequisites — one-time setup

The helper must be installed on the user's Mac before you can use this skill. **Installation is manual** and requires the user to:

1. Clone or download this repository.
2. Run the standard `install.sh` (or optionally `install-hardened.sh` for defense-in-depth).
3. Grant **Full Disk Access** to the exact wrapper path printed by the installer.
4. (For sending) Approve the Automation prompt on first send: *"grokbot-imessage-helper wants to control Messages."*

If the user hasn't installed the helper yet, **direct them to the installation section** in `README.md`, or walk them through it step-by-step. Do not proceed with iMessage actions until the helper is installed and FDA is granted.

---

## Bridge Folder Discovery

Before you can use the helper, you need to know where the bridge folder is located. **Ask the user once** and remember it for the rest of the conversation.

Example prompt:

> "What bridge folder did the iMessage installer print? The hardened default is `~/Library/Application Support/GrokBotIMessage`."

Once the user provides the path, verify it by checking for these files:

```bash
BRIDGE="<user-provided-path>"
ls -la "$BRIDGE/control/requests" "$BRIDGE/control/responses"
```

If any are missing, the helper isn't installed. Guide the user through installation.

Before the first message operation, call `status`. Require protocol major version
`1`; if it differs, stop and tell the user to update the helper and skill together.
If `read_policy.mode` is `allowlist` and `allowlist_entries` is zero, explain that
reads are intentionally disabled and direct the user to the hardened installer's
`configure_allowlist.py` command. Never switch policy modes automatically.

**Store the bridge folder path** in your memory/context for the rest of the conversation. You'll use it for all subsequent request/response operations.

---

## Invoking the Helper (Read/Analyze Actions)

To invoke the helper, you:

1. **Generate a unique request ID** (use a UUID or timestamp).
2. **Write a JSON request file** to `<bridge>/control/requests/request-<id>.json`.
3. **Poll for the response** at `<bridge>/control/responses/response-<id>.json` (typically appears within 2–5 seconds).
4. **Read and parse the response, then delete the response file immediately.** Responses can contain private message text. The helper reaps abandoned responses after one hour, but client deletion is the primary lifecycle.

**Request format:**

```json
{
  "id": "<unique-request-id>",
  "action": "<action-name>",
  "params": { /* action-specific parameters */ }
}
```

**Response format (success):**

```json
{
  "id": "<same-as-request-id>",
  "ok": true,
  "action": "<action-name>",
  /* action-specific response fields */
}
```

**Response format (error):**

```json
{
  "id": "<same-as-request-id>",
  "ok": false,
  "error": "Error message"
}
```

---

## Actions

Full protocol documentation is in `docs/PROTOCOL.md`. Below is a quick reference for each action.

### `status` — Verify compatibility

```json
{"id": "abc123", "action": "status", "params": {}}
```

This action returns helper/protocol versions and local installation checks. It
does not read `chat.db` or return message content.

### `review` — Triage recent messages

**Request:**
```json
{"id": "abc123", "action": "review", "params": {"days": 2}}
```

**Response:** Three buckets: `needs_reply` (actionable, full text + context), `low_priority` (can wait, full text + context), and `skip_summary` (summary only, no text).

Each entry in `needs_reply` and `low_priority` has: `chat_id`, `label`, `contact_name`, `display_name`, `last_ts`, `last_text`, `context` (array of recent messages), and `msg_count`.

**Example shell workflow:**
```bash
BRIDGE="$HOME/imessage-bridge"
REQ_ID=$(uuidgen | tr '[:upper:]' '[:lower:]' | head -c 12)

# IMPORTANT: Write to temp file first, then rename atomically.
# This prevents launchd from firing mid-write and processing incomplete JSON.
TMP="$BRIDGE/control/requests/.request-$REQ_ID.json.tmp"
FINAL="$BRIDGE/control/requests/request-$REQ_ID.json"

cat > "$TMP" <<EOF
{"id": "$REQ_ID", "action": "review", "params": {"days": 2}}
EOF

# Atomic rename publishes the complete request.
mv "$TMP" "$FINAL"

# Poll for response (max 15 seconds)
for i in {1..30}; do
  if [[ -f "$BRIDGE/control/responses/response-$REQ_ID.json" ]]; then
    RESPONSE="$BRIDGE/control/responses/response-$REQ_ID.json"
    cat "$RESPONSE"
    rm -f "$RESPONSE"
    break
  fi
  sleep 0.5
done
```

**Critical:** Always write requests via temp file + atomic rename. Writing directly to `request-*.json` can cause the helper to fire mid-write, leading to JSON parse errors.

---

### `search` — Find messages by substring

**Request:**
```json
{"id": "abc123", "action": "search", "params": {"term": "dinner plans", "days": 30, "limit": 100}}
```

**Response:** Array of `matches` with `chat_id`, `contact_name`, `ts`, `is_from_me`, `text`. Sorted by timestamp (newest first).

---

### `chat_history` — Recent messages in one thread

**Request:**
```json
{"id": "abc123", "action": "chat_history", "params": {"chat": "Angel Vossough", "days": 14, "limit": 100}}
```

`chat` can be:
- Contact name (resolved via Contacts.app)
- Phone number (any format; last 10 digits matched)
- Email address
- **Note:** Group chat IDs (e.g., `chat123456789`) work for read-only actions but are **NOT supported for sending**.

**Response:** `chat_query`, `resolved_substr`, `count`, and `messages` array (each with `chat_id`, `contact_name`, `ts`, `is_from_me`, `text`).

---

### `response_stats` — Average reply time to one contact

**Request:**
```json
{"id": "abc123", "action": "response_stats", "params": {"chat": "Angel Vossough", "hours": 24}}
```

**Response:** `chat_query`, `resolved_substr`, `hours`, `sample_size`, `avg_seconds`, `avg_human`, `median_seconds`, `min_seconds`, `max_seconds`, `total_inbound_messages`, `total_outbound_messages`.

---

### `contacts_lookup` — Find matching contacts

**Request:**
```json
{"id": "abc123", "action": "contacts_lookup", "params": {"name": "Angel"}}
```

**Response:** Array of `matches` with `name`, and either `phone_last10` or `email`. Returns up to 25 matches.

Useful for disambiguating before `chat_history` or `send`.

---

### `send_preview` — Dry-run a send (validation only)

**Request:**
```json
{"id": "abc123", "action": "send_preview", "params": {"to": "+14155551234", "text": "Confirmed for 3pm.", "service": "iMessage"}}
```

`service` can be `"iMessage"`, `"SMS"`, or omitted (defaults to `iMessage`).

**Response:**
```json
{
  "id": "abc123",
  "ok": true,
  "action": "send_preview",
  "preview": {
    "to": "+14155551234",
    "resolved_name": "Alex",
    "service": "iMessage",
    "text": "Confirmed for 3pm.",
    "text_length": 18,
    "blocked": false
  },
  "send_nonce": "Zk9...short-opaque-string",
  "send_nonce_ttl_seconds": 60
}
```

**CRITICAL:** The helper returns a `send_nonce` that you **must** echo back in the subsequent `send` request. The nonce is bound to the exact `(to, text, service)` triple and expires after `send_nonce_ttl_seconds` (default 60s). This enforces the preview-then-confirm gate at the helper level.

`send_preview` does **not** read `chat.db` and does **not** send anything. It only validates the recipient and body, resolves the contact name, and checks the blocklist.

---

### `send` — Actually send the message

**Request:**
```json
{"id": "abc123", "action": "send", "params": {"to": "+14155551234", "text": "Confirmed for 3pm.", "service": "iMessage", "send_nonce": "Zk9...short-opaque-string"}}
```

The `send_nonce` is the one returned by the preceding `send_preview`. The `to`, `text`, and `service` **must** match the preview exactly.

**Response on success:**
```json
{
  "id": "abc123",
  "ok": true,
  "action": "send",
  "sent": {
    "to": "+14155551234",
    "resolved_name": "Alex",
    "service": "iMessage",
    "text_length": 18,
    "sent_at": "2026-08-12T00:15:42"
  }
}
```

**Response on error:**
```json
{"id": "abc123", "ok": false, "error": "send gate: missing nonce; call send_preview first"}
```

Or:
```json
{"id": "abc123", "ok": false, "error": "send cancelled by user or timed out (60s dialog limit)"}
```

The helper writes `text` to a temporary UTF-8 file, shells out to `/usr/bin/osascript` with a short AppleScript, and deletes the tempfile (even on failure).

**Send-gate validation (enforced helper-side):**

- The `send_nonce` must be present, fresh (within TTL), and match the payload.
- Nonces are single-use: consumed on first `send` attempt, deleted on any validation failure.
- Replaying a used nonce, sending without a nonce, or changing the payload after preview all result in rejection before the confirmation dialog appears.
- **After nonce validation succeeds, the helper displays a native macOS dialog** showing the resolved name, exact recipient address, service, and full message text in a scrollable view. Cancel is the keyboard default. The user must deliberately select **Send**; cancelling or waiting 60 seconds aborts the send.
- Text must be 1–4000 chars with no C0 control bytes other than `\n`, `\r`, `\t`.
- Recipient must not be on `contacts/blocked_chats.txt`.
- **Group chat IDs are NOT supported as send targets.** Only individual phone numbers or email addresses work for sending.

---

## Sending Flow (Preview → Confirm → Send)

**Recommended workflow every time:**

1. **Resolve the recipient.** If the user provided a name, call `contacts_lookup` first. If multiple matches, surface them and ask. **Note:** Group chat IDs cannot be used as send targets—only individual phone numbers or email addresses.
2. **Issue `send_preview`.** Show the user:
   - Resolved recipient name
   - Service (iMessage / SMS)
   - Full text and `text_length`
   - Whether `blocked: true` (if so, stop—don't prompt for approval)
3. **Wait for explicit user approval in the chat.** Do not proceed without confirmation.
4. **Issue `send` with the `send_nonce` from step 2.** The `to`, `text`, and `service` must match the preview exactly.
5. **The helper will display a native macOS dialog** for final confirmation. The user must click **Send** in this system dialog.
6. **If the chat approval takes >60s,** re-run `send_preview` to mint a fresh nonce.
7. **Surface `sent.sent_at` and resolved name** as confirmation.

**Example shell workflow:**
```bash
BRIDGE="$HOME/imessage-bridge"
REQ_ID_PREVIEW=$(uuidgen | tr '[:upper:]' '[:lower:]' | head -c 12)

# Step 1: send_preview (atomic write)
TMP="$BRIDGE/control/requests/.request-$REQ_ID_PREVIEW.json.tmp"
FINAL="$BRIDGE/control/requests/request-$REQ_ID_PREVIEW.json"

cat > "$TMP" <<EOF
{"id": "$REQ_ID_PREVIEW", "action": "send_preview", "params": {"to": "+14155551234", "text": "Confirmed for 3pm.", "service": "iMessage"}}
EOF
mv "$TMP" "$FINAL"

# Poll for preview response
for i in {1..30}; do
  if [[ -f "$BRIDGE/control/responses/response-$REQ_ID_PREVIEW.json" ]]; then
    PREVIEW_RESPONSE="$BRIDGE/control/responses/response-$REQ_ID_PREVIEW.json"
    PREVIEW=$(cat "$PREVIEW_RESPONSE")
    rm -f "$PREVIEW_RESPONSE"
    echo "$PREVIEW"
    break
  fi
  sleep 0.5
done

# Extract send_nonce from preview response (use jq or similar)
SEND_NONCE=$(echo "$PREVIEW" | jq -r '.send_nonce')

# Show preview to user, wait for approval...

# Step 2: send (after user approval, atomic write)
REQ_ID_SEND=$(uuidgen | tr '[:upper:]' '[:lower:]' | head -c 12)
TMP="$BRIDGE/control/requests/.request-$REQ_ID_SEND.json.tmp"
FINAL="$BRIDGE/control/requests/request-$REQ_ID_SEND.json"

cat > "$TMP" <<EOF
{"id": "$REQ_ID_SEND", "action": "send", "params": {"to": "+14155551234", "text": "Confirmed for 3pm.", "service": "iMessage", "send_nonce": "$SEND_NONCE"}}
EOF
mv "$TMP" "$FINAL"

# Poll for send response
for i in {1..30}; do
  if [[ -f "$BRIDGE/control/responses/response-$REQ_ID_SEND.json" ]]; then
    SEND_RESPONSE="$BRIDGE/control/responses/response-$REQ_ID_SEND.json"
    cat "$SEND_RESPONSE"
    rm -f "$SEND_RESPONSE"
    break
  fi
  sleep 0.5
done
```

---

## Common Pitfalls

- **FDA not granted yet.** First request returns `sqlite3.OperationalError: unable to open database file` in `control/log.txt`. Tell the user to grant FDA to the exact wrapper path printed by their installer.
- **Wrapper re-signed.** If the user rebuilt the wrapper, the FDA grant needs to be removed and re-added (macOS identifies the binary by CDHash, not path).
- **Ambiguous contact name.** `chat: "Alex"` resolves via the first contact whose name contains "Alex"—may not be the intended one. Fall back to phone number if the user has multiple Alexes.
- **Group chats.** Group chat IDs look like `chat1234567…`. The `review` and `chat_history` actions accept these, but **sending to group chats is NOT supported**. Use individual phone numbers or emails for sending. Attempting to send to a `chatNNNN` identifier will be rejected at the preview stage.
- **Wrong folder selected.** If the user tells you a new bridge folder path, re-verify that the helper is installed there before proceeding.

---

## Redaction and Privacy

The helper redacts before writing the response:

- 2FA / verification codes near words like "code", "verification", "OTP"
- Credit-card-like digit runs (13–19 digits)
- US SSN patterns

The active read policy is applied before response JSON is written. The hardened
default returns only root-allowlisted chats; the blocklist always removes listed
threads. Treat redaction as a second line of defense.

**Known redaction gaps** (see `docs/PROTOCOL.md` for full list):
- Dot-separated credit cards
- PIN-labelled codes
- API keys
- Bank account numbers
- Home addresses
- Dates of birth

The read policy is the reliable filter; redaction is a second line of defense.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Requests pile up in `control/requests/`, no responses | FDA not granted yet | Grant FDA to the wrapper binary (path printed by `install.sh`) |
| `sqlite3.OperationalError: unable to open database file` in `log.txt` | FDA not granted, or grant stale | Re-add the wrapper in System Settings → Full Disk Access |
| First send fails with Automation prompt | macOS needs Automation permission | Click **OK** on the prompt; future sends will work |
| `send gate: missing nonce` | `send` called without prior `send_preview` | Always call `send_preview` first and pass the returned nonce |
| `send payload differs from preview` | Body or recipient changed after preview | Re-run `send_preview` to mint a fresh nonce for the new payload |
| Messages decode as empty | `attributedBody` parser failed | Check `control/log.txt` for a byte-count-only parser diagnostic |

Check `<bridge>/control/log.txt` first when debugging. It contains helper stderr and logging.

---

## What the Helper Won't Do

- **No attachments, images, stickers, audio, Tapback reactions.** Text fields only.
- **No message editing or deletion.** Once sent, a message is immutable from the helper's perspective.
- **No message effects** (balloons, confetti, invisible ink).
- **No group-chat sending.** Group chat IDs can be read (`review`, `chat_history`), but **cannot be used as send targets**. Sending requires individual phone numbers or email addresses.
- **No group-chat creation.** Cannot create new group chats.
- **Only reads local `chat.db`.** If a thread hasn't synced to this Mac, it won't appear in search/review.

---

## Security Notes

- The bridge folder is mode `700` (user-only access).
- The helper runs with **Full Disk Access**—a bug or compromise becomes a full-user-file-read primitive.
- Nonces are single-use and expire after 60s. A process that can write to the bridge folder can still exfiltrate message content by crafting a read request, but it cannot silently send without racing a real user-approved preview.
- See `SECURITY.md` for full threat-model details.

---

## References

- **Full protocol documentation:** `docs/PROTOCOL.md`
- **Smoke test:** `docs/SMOKE_TEST.md`
- **Security details:** `SECURITY.md`

---

## License

This skill is part of the `grokbot-imessage-skill` project, licensed under MIT.
