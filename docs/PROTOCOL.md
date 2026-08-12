# iMessage Helper Bridge Protocol

This document describes the JSON-based request/response protocol that both Claude Cowork and Grok Bot use to communicate with the macOS helper.

## Architecture

```
  AI Host (agent context)              macOS (your local machine)
  -----------------------              -------------------------
  Writes request-<id>.json  -->  launchd watches control/requests/  -->
  Reads  response-<id>.json  <-- cowork-imessage-helper (wrapper)   -->
                                 helper.py (FDA-granted, reads
                                 chat.db + AddressBook, drives osascript)
```

The AI host runs in a sandboxed environment (Linux for Cowork, potentially remote for Grok Bot) that cannot directly access `~/Library/Messages/chat.db`. A launchd agent bridges the two sides:

1. The AI host writes a JSON request file to `<bridge-folder>/control/requests/request-<id>.json`
2. launchd fires the helper within ~1 second (WatchPaths with 1s ThrottleInterval)
3. The helper reads the request, processes it, and writes a response to `<bridge-folder>/control/responses/response-<id>.json`
4. The AI host polls for and reads the response (typically 2–5s total)

## Bridge Folder Layout

After installation, the bridge folder contains:

```
<bridge-folder>/
├── bin/
│   ├── cowork-imessage-helper      # Compiled C wrapper (FDA target)
│   ├── helper.py                    # Python worker (reads chat.db, calls osascript)
│   └── send_gate.py                 # Nonce validation for sends
├── control/
│   ├── requests/                    # AI host writes here
│   ├── responses/                   # Helper writes here
│   └── log.txt                      # Helper stderr + logging
├── contacts/
│   └── blocked_chats.txt            # User-maintained blocklist
├── nonces/                          # Short-lived send-preview nonces
├── install.sh
├── uninstall.sh
└── com.user.cowork-imessage.plist.template
```

## Request Format

All requests are JSON files with this structure:

```json
{
  "id": "<unique-request-id>",
  "action": "<action-name>",
  "params": { /* action-specific parameters */ }
}
```

Use a UUID or timestamp for `id`. The `id` must be unique within the bridge folder's lifetime.

## Response Format

All responses are JSON files with this structure:

```json
{
  "id": "<same-as-request-id>",
  "ok": true,
  "action": "<action-name>",
  /* action-specific response fields */
}
```

On error:

```json
{
  "id": "<same-as-request-id>",
  "ok": false,
  "error": "Error message"
}
```

## Actions

### `review` — Triage recent messages

**Request:**
```json
{
  "id": "abc123",
  "action": "review",
  "params": {
    "days": 2
  }
}
```

**Response:**
```json
{
  "id": "abc123",
  "ok": true,
  "action": "review",
  "needs_reply": [
    {
      "chat_display_name": "Angel Vossough",
      "chat_identifier": "+14155551234",
      "last_message_time": "2026-08-10T14:32:15",
      "last_message_text": "Are we still on for Thursday?",
      "last_message_is_from_me": false,
      "unread_count": 1
    }
  ],
  "low_priority": [ /* same structure */ ],
  "skip_summary": {
    "count": 12,
    "reason": "mostly newsletters and notifications"
  }
}
```

Sorts threads into three buckets: `needs_reply` (actionable, full text included), `low_priority` (can wait, full text included), and `skip_summary` (summary only, no text).

---

### `search` — Find messages by substring

**Request:**
```json
{
  "id": "abc123",
  "action": "search",
  "params": {
    "term": "dinner plans",
    "days": 30,
    "limit": 100
  }
}
```

**Response:**
```json
{
  "id": "abc123",
  "ok": true,
  "action": "search",
  "matches": [
    {
      "chat_display_name": "Alice",
      "chat_identifier": "+14155551234",
      "message_text": "Let's finalize dinner plans for Friday",
      "message_date": "2026-08-05T18:22:00",
      "is_from_me": false
    }
  ]
}
```

Case-insensitive substring search across all threads. Results are sorted by date (newest first).

---

### `chat_history` — Recent messages in one thread

**Request:**
```json
{
  "id": "abc123",
  "action": "chat_history",
  "params": {
    "chat": "Angel Vossough",
    "days": 14,
    "limit": 100
  }
}
```

`chat` accepts:
- Contact name (resolved via Contacts.app)
- Phone number (any format; last 10 digits are matched)
- Email address
- Group chat ID (e.g., `chat123456789`)

**Response:**
```json
{
  "id": "abc123",
  "ok": true,
  "action": "chat_history",
  "chat_display_name": "Angel Vossough",
  "chat_identifier": "+14155551234",
  "messages": [
    {
      "message_date": "2026-08-10T14:32:15",
      "text": "Are we still on for Thursday?",
      "is_from_me": false
    },
    {
      "message_date": "2026-08-10T14:35:00",
      "text": "Yes! See you at 3pm",
      "is_from_me": true
    }
  ]
}
```

---

### `response_stats` — Average reply time to one contact

**Request:**
```json
{
  "id": "abc123",
  "action": "response_stats",
  "params": {
    "chat": "Angel Vossough",
    "hours": 24
  }
}
```

**Response:**
```json
{
  "id": "abc123",
  "ok": true,
  "action": "response_stats",
  "chat_display_name": "Angel Vossough",
  "chat_identifier": "+14155551234",
  "sample_size": 15,
  "avg_seconds": 1098,
  "avg_human": "18.3m",
  "median_seconds": 480,
  "min_seconds": 12,
  "max_seconds": 7200,
  "inbound_count": 23,
  "outbound_count": 19
}
```

Computes reply-time statistics over the specified window.

---

### `contacts_lookup` — Find matching contacts

**Request:**
```json
{
  "id": "abc123",
  "action": "contacts_lookup",
  "params": {
    "name": "Angel"
  }
}
```

**Response:**
```json
{
  "id": "abc123",
  "ok": true,
  "action": "contacts_lookup",
  "matches": [
    {
      "display_name": "Angel Vossough",
      "phone_numbers": ["+14155551234"],
      "emails": ["angel@example.com"]
    }
  ]
}
```

Searches Contacts.app by name. Useful for disambiguating before `chat_history` or `send`.

---

### `send_preview` — Dry-run a send (validation only)

**Request:**
```json
{
  "id": "abc123",
  "action": "send_preview",
  "params": {
    "to": "+14155551234",
    "text": "Confirmed for 3pm.",
    "service": "iMessage"
  }
}
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

**Critical:** The helper returns a `send_nonce` that **must** be echoed back in the subsequent `send` request. The nonce is bound to the exact `(to, text, service)` triple and expires after `send_nonce_ttl_seconds` (default 60s). This enforces the preview-then-confirm gate at the helper level.

`send_preview` does **not** read `chat.db` and does **not** send anything. It only validates the recipient and body, resolves the contact name, and checks the blocklist.

---

### `send` — Actually send the message

**Request:**
```json
{
  "id": "abc123",
  "action": "send",
  "params": {
    "to": "+14155551234",
    "text": "Confirmed for 3pm.",
    "service": "iMessage",
    "send_nonce": "Zk9...short-opaque-string"
  }
}
```

The `send_nonce` is the one returned by the preceding `send_preview`. The `to`, `text`, and `service` **must** match the preview exactly. If any of these differ, the helper rejects the request with a `"send payload differs from preview"` error.

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
{
  "id": "abc123",
  "ok": false,
  "error": "send gate: missing nonce; call send_preview first"
}
```

The helper writes `text` to a temporary UTF-8 file, shells out to `/usr/bin/osascript` with a short AppleScript, and deletes the tempfile (even on failure).

**Send-gate validation (enforced helper-side):**

- The `send_nonce` must be present, fresh (within TTL), and match the payload.
- Nonces are single-use: consumed on first `send` attempt, deleted on any validation failure.
- Replaying a used nonce, sending without a nonce, or changing the payload after preview all result in rejection before `osascript` runs.
- Text must be 1–4000 chars with no C0 control bytes other than `\n`, `\r`, `\t`.
- Recipient must not be on `contacts/blocked_chats.txt`.

---

## Redaction

Before returning any response that includes message text, the helper runs a regex-based redactor that masks:

- 2FA / verification codes (near keywords like `code`, `verification`, `OTP`, `passcode`)
- Credit-card-like digit runs (13–19 digits)
- US SSN patterns (`NNN-NN-NNNN`)

Redacted content is replaced with `[REDACTED]`.

**Known gaps** (see `tests/test_redaction.py::RedactionKnownBypasses`):
- Dot-separated credit cards (`4111.1111.1111.1111`)
- PIN-labelled codes (`Your PIN is 4829`)
- Slash-separated SSNs (`123/45/6789`)
- Bare codes with no keyword (`839201 to confirm`)
- API keys (Stripe, GitHub, OpenAI tokens)
- Bank account / routing numbers
- Home addresses
- Dates of birth

The thread-level blocklist (see below) is the reliable filter; redaction is a second line of defense.

---

## Blocklist

`contacts/blocked_chats.txt` is checked **before** the redactor runs. Threads on the blocklist are dropped entirely—their text never enters the response JSON.

Format: one entry per line. Lines starting with `#` are ignored.

**Matches:**
- **Phone numbers:** last 10 digits compared (e.g., `+1-555-123-4567`, `5551234567`, `(555) 123-4567` all match)
- **Email addresses:** full case-insensitive match
- **Group chat IDs:** anything starting with `chat` or containing a distinctive substring

**Example:**
```
# Therapist
+15551234567

# Attorney
lawyer@example.com

# Family group chat
chat123456789
```

Blocked threads are enforced for **both** inbound (read actions) and outbound (`send` actions).

---

## Typical Request Flow (Pseudocode)

```python
import json, uuid, time, pathlib

bridge = pathlib.Path("<bridge-folder>")
req_id = uuid.uuid4().hex[:12]

# Write request
(bridge / "control" / "requests" / f"request-{req_id}.json").write_text(
    json.dumps({"id": req_id, "action": "review", "params": {"days": 2}})
)

# Poll for response
resp_path = bridge / "control" / "responses" / f"response-{req_id}.json"
for _ in range(30):  # 15-second timeout
    if resp_path.exists():
        break
    time.sleep(0.5)

# Read response
data = json.loads(resp_path.read_text())
if data["ok"]:
    print(data)
else:
    print(f"Error: {data['error']}")
```

---

## Sending Flow (Preview → Confirm → Send)

**Recommended workflow:**

1. **Resolve the recipient.** If the user provided a name, call `contacts_lookup` first. If multiple matches, surface them and ask.
2. **Issue `send_preview`.** Show the user:
   - Resolved recipient name
   - Service (iMessage / SMS)
   - Full text and `text_length`
   - Whether `blocked: true` (if so, stop—don't prompt for approval)
3. **Wait for explicit user approval.** Do not proceed without confirmation.
4. **Issue `send` with the `send_nonce` from step 2.** The `to`, `text`, and `service` must match the preview exactly.
5. **If approval takes >60s,** re-run `send_preview` to mint a fresh nonce.
6. **Surface `sent.sent_at` and resolved name** as confirmation.

---

## Permissions Required

### Full Disk Access (FDA)

Required to read `~/Library/Messages/chat.db`. The FDA grant is attached to the C wrapper's CDHash, not its path. Changing the source or compiler can produce a different CDHash, requiring re-grant.

**Grant location:**
```
System Settings → Privacy & Security → Full Disk Access
→ Add: <bridge-folder>/bin/cowork-imessage-helper
```

### Automation → Messages

Required to send. First send triggers a one-time prompt: *"cowork-imessage-helper wants to control Messages."* Click **OK**.

**Grant location:**
```
System Settings → Privacy & Security → Automation
→ cowork-imessage-helper → Messages (toggle on)
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Requests pile up in `control/requests/`, no responses | FDA not granted yet | Grant FDA to the wrapper binary (path printed by `install.sh`) |
| `sqlite3.OperationalError: unable to open database file` in `log.txt` | FDA not granted, or grant stale | Re-add the wrapper in System Settings → Full Disk Access |
| First send fails with Automation prompt | macOS needs Automation permission | Click **OK** on the prompt; future sends will work |
| `send gate: missing nonce` | `send` called without prior `send_preview` | Always call `send_preview` first and pass the returned nonce |
| `send payload differs from preview` | Body or recipient changed after preview | Re-run `send_preview` to mint a fresh nonce for the new payload |
| Messages decode as empty | `attributedBody` parser failed | Check `control/log.txt`—helper logs the first 64 bytes of unparseable blobs |

---

## What the Helper Won't Do

- **No attachments, images, stickers, audio, Tapback reactions.** Text fields only.
- **No message editing or deletion.** Once sent, a message is immutable from the helper's perspective.
- **No message effects** (balloons, confetti, invisible ink).
- **No group-chat creation.** Can send *to* an existing group chat ID, but not stand one up.
- **Only reads local `chat.db`.** If a thread hasn't synced to this Mac, it won't appear in search/review.

---

## Files and Directories

| Path | Role |
|------|------|
| `control/requests/` | AI host writes request JSON here. Watched by launchd. |
| `control/responses/` | Helper writes response JSON here. AI host reads. |
| `control/log.txt` | Helper stderr + logging. First place to check when debugging. |
| `contacts/blocked_chats.txt` | User-maintained blocklist of sensitive chats. |
| `bin/cowork-imessage-helper` | Compiled, ad-hoc signed C wrapper (the FDA target). |
| `bin/helper.py` | Python worker (reads chat.db, resolves contacts, redacts, drives osascript). |
| `bin/send_gate.py` | Nonce minting and validation for send-preview/send gate. |
| `nonces/` | Short-lived per-nonce files bound to previewed sends. TTL-reaped on every helper run. |

---

## Security Notes

- The bridge folder should be mode `700` (user-only access).
- The helper runs with **Full Disk Access**—a bug or compromise becomes a full-user-file-read primitive.
- Nonces are single-use and expire after 60s. A process that can write to the bridge folder can still exfiltrate message content by crafting a read request, but it cannot silently send without racing a real user-approved preview.
- See `SECURITY.md` for full threat-model details.

---

## License

This protocol is part of the `imessage-review` project, licensed under MIT.
