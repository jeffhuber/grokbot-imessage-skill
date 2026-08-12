# Security

This document describes what the `grokbot-imessage-skill` helper does with your Mac's
permissions, the trust boundaries it relies on, and what to do if you
find a vulnerability.

It is written to be specific about limitations rather than reassuring.
If something below sounds too permissive for your threat model, don't
install the helper.

## What this project does

`grokbot-imessage-skill` is an iMessage integration for Grok Bot (and compatible AI assistants) that:

- Reads your local Messages database (`~/Library/Messages/chat.db`) so
  your AI assistant can search, summarize, and surface messages that need
  a reply.
- Optionally sends iMessages on your behalf, gated behind a
  preview-and-confirm step.

Both paths go through a single on-device helper process: a Python
script (`helper.py`) launched by an ad-hoc-signed C wrapper via a
user-scoped `launchd` LaunchAgent. The C wrapper exists solely to give
the helper a stable `CDHash`, which is what macOS TCC uses to identify
the process holding Full Disk Access.

## Permissions required, and what each one actually grants

### Full Disk Access (FDA)

Required to read `chat.db`. macOS does not offer a narrower grant —
FDA is the coarsest of the TCC permissions and is functionally "read
anything this user can read."

Concretely, FDA on this helper gives the helper process the ability
to read:

- The entire Messages database, including attachments.
- Other apps' protected storage (Mail, Safari history, calendars,
  `Library/Application Support/*`).
- Any user file that isn't itself TCC-gated.

The plugin *code* only reads `chat.db`. But a bug or compromise in
the helper becomes a full-user-file-read primitive, not just a
Messages leak. Treat that as the blast radius.

### The CDHash pins the wrapper, not the Python

The TCC grant for Full Disk Access is bound to the C wrapper's
CDHash. That stabilizes the grant across OS updates that would
otherwise re-hash `/usr/bin/python3`, but it does not extend any
authentication to `helper.py` itself. The wrapper's one and only job
is to `exec` `helper.py`, and `helper.py` lives in the user-writable
bridge folder (e.g., `~/imessage-bridge/bin/helper.py`, mode 600 but owned
by you).

That means any process running as your user with write access to the
bridge folder can overwrite `helper.py` with its own Python, and the
very next `launchd` trigger will run that replacement under the
wrapper's CDHash and inherit FDA. A tampered `helper.py` would:

- Bypass the v0.4.0 helper-side send gate — a replacement simply
  wouldn't check nonces.
- Exfiltrate message content via any channel the attacker chose:
  stdout, an HTTP POST, a file they read later.
- Silently defeat any future read-path authentication (HMAC or
  otherwise), because authentication is enforced by the helper, and
  the helper has been replaced.

This is the same threat class covered by the "malicious supply-chain
packages running as your user" caveat below — a process that can
write into your `$HOME` can compromise the plugin by replacing
`helper.py`, full stop. The helper-side gates are defense in depth
against accidents and unsophisticated attackers; they are not a
barrier against an attacker with user-UID write access to the bridge
folder.

A stricter posture — installing `helper.py` to a root-owned path
like `/usr/local/libexec/cowork-imessage/helper.py` via `sudo` in
`install.sh` — is a plausible future mitigation but is not currently
shipped. Until then, treat the bridge folder as part of the trusted
compute base: if something can write there, it owns the helper.

### Automation → Messages (v0.3.0+)

Required to send. The first send triggers a one-time macOS prompt:
"cowork-imessage-helper wants to control Messages." The grant lives
under System Settings → Privacy & Security → Automation →
cowork-imessage-helper → Messages.

Concretely, this grants the helper the ability to:

- Send iMessages or SMS to any recipient, from any of your
  iMessage-enabled addresses.
- Query Messages.app AppleScript state (services, buddies, chats).

It does NOT grant access to attachments, effects, edit/delete, or
group-chat creation — those aren't in the AppleScript surface.

## Trust boundaries

The helper communicates with Grok Bot (or other AI hosts) via a **bridge folder** — a
mode-700 directory under the user's home directory (e.g., `~/imessage-bridge/`)
where the AI writes request files and the helper writes response
files. `launchd`'s `WatchPaths` triggers the helper on change.

This is the primary trust boundary you need to understand.

**What the bridge folder protects against:**

- Other user accounts on the same machine. The folder is `700`; only
  the owning UID can read or write.
- Sandboxed apps that don't have access to `$HOME`.

**What the bridge folder does NOT protect against:**

- Any unsandboxed process running as your user. If you run a malicious
  `npm install`, `pip install`, `brew install`, or anything else that
  executes as your UID, that process can:
  - Write a read request into the bridge folder and receive message
    contents in response.
  - Write a `send` request. **(See the confirmation gate section below
    — since v0.4.0 a lone `send` request without a preceding preview is
    rejected helper-side. The attacker would also have to forge a
    `send_preview` that shows up in Claude's UI, or race the 60-second
    window after a real one.)**

This is the central limitation of the current design on the **read**
path: a process running as your user can exfiltrate message content.
An HMAC-authenticated envelope that binds request files to a
per-install key is planned for v0.4.1. If your threat model includes
malicious supply-chain packages running as your user, do not install
this plugin in its current form.

## Confirmation gate (sending)

Sending is confirmation-gated via a two-layer preview/confirm protocol:

**Layer 1: Nonce validation (v0.4.0+)**

1. The AI asks the helper for a `send_preview` — the helper does
   NOT send; it echoes back the normalized payload and mints a
   one-shot **send nonce** bound to exactly that `(to, text, service)`
   triple.
2. Grok Bot shows the preview to you in chat; you approve.
3. The AI asks the helper to `send`, passing the nonce from step 1.
   The helper recomputes the payload hash, compares it to the nonce's
   stored hash.

**Layer 2: Native macOS dialog (v1.0.0+)**

4. After nonce validation succeeds, the helper displays a **native macOS
   system dialog** showing:
   - Recipient (resolved contact name if available)
   - Service (iMessage or SMS)
   - Truncated message text (first 200 chars)
5. You must click **Send** to proceed. Clicking **Cancel** or waiting
   60 seconds aborts the send.
6. Only after the dialog is confirmed does the helper call `osascript`
   to send.

This two-layer gate is enforced **helper-side**. A process that writes
directly to the bridge folder and issues a `send` with no nonce, a forged
nonce, a replayed (already-consumed) nonce, an expired nonce (TTL is 60
seconds), or a nonce whose bound payload differs from the `send` request's
`(to, text, service)` is rejected before the dialog appears. Nonces are
stored as per-file records under `~/imessage-bridge/nonces/` (mode 600),
are single-use (deleted on consume), and are also deleted on any
validation failure so the same nonce cannot be retried with a corrected
payload.

An attacker who can write to the bridge folder would need to:
1. Race a real, user-approved preview inside its 60-second window *and*
   send the exact same payload the user saw in chat — they cannot
   silently swap the recipient or body.
2. Wait for the victim to click **Send** in the native macOS dialog that
   appears on their screen. The dialog shows recipient, service, and
   message preview; the victim would see the attack payload.

The v0.3.x AI-side check still runs as well; the helper-side nonce gate
and native dialog are defense in depth, not a replacement.

**Behavior change for existing Cowork helpers:** The native dialog is new
in v1.0.0. Existing helpers from the sibling `claudecowork-imessage-skill`
repository (v0.4.0 and earlier) do not show the dialog. Users who upgrade
to this helper will experience the added confirmation step.

## Blocklist

`contacts/blocked_chats.txt` is checked by the helper before any read
or send involving a listed identifier. The list is editable by the
user and is honored for both inbound (search/review) and outbound
(send) as of v0.3.0.

The blocklist is best-effort. It is not a privacy boundary — anyone
who can edit the file can also remove entries, and the helper trusts
the list verbatim. Use it to prevent accidental exposure, not as a
security control.

## What leaves the machine

The helper itself does not make any outbound network connections. All
message content read from `chat.db` or sent via AppleScript is
processed on-device by the helper.

When Grok Bot uses this skill, message content that Grok Bot reads
passes through xAI's normal pipeline, which means it reaches
xAI's servers as part of the conversation, subject to
xAI's standard data-handling terms. If you don't want a specific
conversation touched, add the identifier to the blocklist or don't
invoke the skill on that range.

The plugin does **not**:

- Send telemetry.
- Phone home.
- Auto-update.
- Log message content to disk outside of the short-lived `chat.db`
  copy used for reads (see below).

## The chat.db copy

SQLite locks `chat.db` while Messages.app has it open, so the helper
copies it to a per-request tempfile under the user's cache directory,
reads the copy, and deletes it at the end of the request. The copy is
mode-600 and is cleaned up on normal exit; an abnormal exit (OOM,
SIGKILL) can leave a stale copy behind.

`send` actions do NOT copy `chat.db` — a `needs_db` flag on each
request handler short-circuits the copy for write-only operations.

## Third-party privacy

Messages are two-sided. Every message this plugin reads was sent to
or from someone else, and they never consented to have their words
processed by an LLM. If you use this plugin, you are making that
choice on their behalf.

This is not a flaw in the code; it's an intrinsic property of giving
an assistant access to your messages. It's mentioned here because it's
a legitimate concern that the README should not bury.

## Durability

This plugin depends on two Apple surfaces that are not contractually
stable:

- Direct read access to `~/Library/Messages/chat.db`. Apple has
  tightened TCC over several macOS releases and could close this
  further.
- AppleScript control of Messages.app. AppleScript support across
  Apple's own apps has been trending down for years.

Either could be deprecated in a future macOS release. If that
happens, this plugin will need to be rewritten or will stop working.

## Auditing this helper

You can verify what's actually on your disk:

- All source is in this repository. Read `bin/helper.py` — it's pure
  Python and the only thing that runs with FDA + Automation-over-Messages.
- The C wrapper source is in `bin/cowork_imessage_helper.c`. It does
  approximately nothing — it exists to stabilize the CDHash. You can
  rebuild it yourself; the README documents the one-line `clang` command
  used by `install.sh`.
- Verify the repository contents match what's on GitHub. Each
  GitHub release is tagged; the source is small enough to diff against
  a local clone.
- After install, verify the LaunchAgent plist under
  `~/Library/LaunchAgents/com.user.cowork-imessage.plist` points only
  at the wrapper in the bridge folder and carries no other arguments.

## Revoking

To fully remove the helper's access:

1. Run `./uninstall.sh` in the bridge folder.
2. Delete the bridge folder: `rm -rf ~/imessage-bridge` (or wherever
   you installed it).
3. System Settings → Privacy & Security → Full Disk Access → remove
   `cowork-imessage-helper`.
4. System Settings → Privacy & Security → Automation →
   cowork-imessage-helper → turn Messages off (or remove the entry
   entirely).

## Reporting a vulnerability

If you find a security issue, please do NOT open a public GitHub
issue. Email <jhuber+grokbotimessage@gmail.com> with details and, if possible, a
minimal reproduction. I will acknowledge within a few days and
coordinate disclosure.
