# Smoke Test — 5-Minute Validation

This checklist verifies that the iMessage helper is installed correctly and working with Grok Bot.

Run this after initial installation or after rebuilding the helper.

---

## Prerequisites

- [ ] Helper is installed (`install.sh` completed successfully)
- [ ] Full Disk Access granted to `<bridge-folder>/bin/cowork-imessage-helper`
- [ ] Grok Bot has access to execute shell commands on your Mac

---

## Test 1: Bridge Folder Setup

**Verify the bridge folder structure exists:**

```bash
BRIDGE="<your-bridge-folder-path>"  # e.g., ~/imessage-bridge
ls -la "$BRIDGE/control/requests"
ls -la "$BRIDGE/control/responses"
ls -la "$BRIDGE/bin/cowork-imessage-helper"
ls -la "$BRIDGE/contacts/blocked_chats.txt"
```

**Expected:**
- All directories and files exist
- `control/requests/` and `control/responses/` are mode `700`
- `bin/cowork-imessage-helper` is an executable binary
- `contacts/blocked_chats.txt` exists (may be empty or template-only)

**If this fails:** Re-run `install.sh`.

---

## Test 2: Launchd Agent Is Running

**Check the launchd agent status:**

```bash
launchctl list | grep com.user.cowork-imessage
```

**Expected:** One line of output like:

```
12345   0   com.user.cowork-imessage
```

The first number is the PID (can be `-` if the agent is loaded but not currently running). The second number (`0`) is the last exit status—`0` means success.

**If this fails:**
- Verify the plist exists: `ls -l ~/Library/LaunchAgents/com.user.cowork-imessage.plist`
- Bootstrap manually: `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.user.cowork-imessage.plist`

---

## Test 3: Read Action (Manual)

**Manually write a test request:**

```bash
BRIDGE="<your-bridge-folder-path>"
REQ_ID=$(date +%s)

TMP="$BRIDGE/control/requests/.request-$REQ_ID.json.tmp"
FINAL="$BRIDGE/control/requests/request-$REQ_ID.json"

cat > "$TMP" <<EOF
{"id": "$REQ_ID", "action": "contacts_lookup", "params": {"name": "test"}}
EOF
mv "$TMP" "$FINAL"
```

**Poll for the response (max 10 seconds):**

```bash
for i in {1..20}; do
  if [[ -f "$BRIDGE/control/responses/response-$REQ_ID.json" ]]; then
    RESPONSE="$BRIDGE/control/responses/response-$REQ_ID.json"
    cat "$RESPONSE"
    rm -f "$RESPONSE"
    break
  fi
  sleep 0.5
done
```

**Expected:**
- A JSON response appears within 2–5 seconds.
- The response has `"ok": true` or `"ok": false` (with an error message if your Contacts app has no matching entries—that's fine for this test).

**If this fails:**
- Check `$BRIDGE/control/log.txt` for errors.
- Verify FDA is granted (System Settings → Privacy & Security → Full Disk Access).
- If `log.txt` shows `sqlite3.OperationalError: unable to open database file`, FDA is missing or stale.

---

## Test 4: Grok Bot Integration (Read)

Before reading messages, run `grok inspect` and confirm `imessage-grok-bot` is
listed. Then issue a `status` request and confirm `protocol_version` begins with
`1` and the installation checks are true.

**Ask Grok Bot to triage recent messages:**

"Triage my iMessages from the last day."

**Expected:**
- Grok Bot asks where your bridge folder is (if first time).
- Grok Bot writes a request file, polls for the response, and presents the triage (needs-reply / low-priority / skipped buckets).
- Grok Bot does **not** say "I can't access your iMessages" or "helper not found."

**If this fails:**
- Verify you've provided the correct bridge folder path to Grok Bot.
- Check `$BRIDGE/control/log.txt` for errors.

---

## Test 5: Sending (Preview Only)

**Ask Grok Bot to preview a send:**

"Preview sending a message to +15551234567 that says 'Test message.'"

(Use a real phone number if you have one handy, or a fake one for this test—preview doesn't actually send.)

**Expected:**
- Grok Bot writes a `send_preview` request, polls for the response, and shows you:
  - Resolved recipient name (if in your Contacts) or the raw number/email
  - Service (iMessage or SMS)
  - Text and text length
  - Whether the recipient is blocked (`blocked: false` for this test number)
  - A `send_nonce` (opaque string)
- Grok Bot does **not** actually send anything yet.

**If this fails:**
- Check `$BRIDGE/control/log.txt` for errors.
- Verify the skill is correctly handling `send_preview` requests.

---

## Test 6: Sending (Full Flow — Optional)

**Only run this if you want to test an actual send.** Use a real recipient you're comfortable texting.

**Ask Grok Bot to send a message:**

"Text +15551234567: 'This is a test message from the iMessage helper.'"

**Expected:**
1. Grok Bot runs `send_preview` and shows you the preview.
2. Grok Bot waits for your explicit approval.
3. You confirm.
4. Grok Bot runs `send` with the `send_nonce` from the preview.
5. **The helper displays a native macOS confirmation dialog** showing the exact recipient and full message text in a scrollable view. Cancel is the keyboard default; deliberately select **Send** to proceed.
6. You see a confirmation: `sent_at` timestamp and resolved recipient name.
7. The message appears in Messages.app on your Mac as sent.

**On first send only:** macOS will prompt: *"cowork-imessage-helper wants to control Messages."* Click **OK**. This grants the Automation permission.

**If this fails:**
- **Automation prompt denied:** Re-enable in System Settings → Privacy & Security → Automation → cowork-imessage-helper → Messages.
- **`send gate: missing nonce`:** Grok Bot didn't call `send_preview` first. This is a skill bug—report it.
- **`send payload differs from preview`:** Grok Bot changed the body or recipient between preview and send. Re-run the preview.

---

## Test 7: Check Logs

**Read the helper logs:**

```bash
tail -n 50 "$BRIDGE/control/log.txt"
```

**Expected:**
- The log may be empty on a healthy run, or may contain timestamped
  diagnostics such as `contacts: loaded ...`.
- No repeated errors or stack traces.

**If you see errors:**
- `sqlite3.OperationalError: unable to open database file` → FDA not granted or stale.
- `AttributeError: ... AddressBook` → Contacts.app database schema changed (rare; report this).
- `OSError: [Errno 2] No such file or directory: '/usr/bin/osascript'` → osascript missing (should never happen on macOS; reinstall Command Line Tools).

---

## All Tests Pass?

If you've reached this point and all tests passed:

✅ **Your iMessage helper is installed correctly and working.**

You can now use Grok Bot to:
- Triage recent messages
- Search message history
- Pull conversation threads
- Compute response-time stats
- Send plain-text iMessages with preview-and-confirm safety

---

## Common Fixes

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| No response files appear | FDA not granted | Grant FDA to `<bridge>/bin/cowork-imessage-helper` in System Settings |
| `sqlite3.OperationalError` in logs | FDA not granted or stale | Re-add the wrapper in System Settings → Full Disk Access |
| Send fails on first attempt | Automation permission needed | Click **OK** on the macOS prompt; future sends will work |
| `send gate: missing nonce` | Skill didn't call `send_preview` first | Report a bug—the skill should always preview before send |
| Messages decode as empty | `attributedBody` parser failed | Check logs for a byte-count-only parser diagnostic |

---

## Next Steps

- Add sensitive threads to `contacts/blocked_chats.txt` (therapists, attorneys, family, etc.)
- Explore chained workflows: "Triage the last 3 days, then draft replies to anything actionable."
- Read the full protocol: `docs/PROTOCOL.md`
- Review security details: `SECURITY.md`

---

## Reporting Issues

If a test fails and the fix isn't obvious:

1. Collect `$BRIDGE/control/log.txt`.
2. Note which test failed and what the actual output was.
3. Open an issue at [github.com/jeffhuber/grokbot-imessage-skill/issues](https://github.com/jeffhuber/grokbot-imessage-skill/issues) with:
   - macOS version
   - Grok Bot version (if applicable)
   - Test number that failed
   - Relevant log excerpts (redact personal info)

---

## License

This smoke test is part of the `grokbot-imessage-skill` project, licensed under MIT.
