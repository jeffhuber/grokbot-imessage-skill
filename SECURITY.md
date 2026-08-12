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
script (`helper.py`) launched by a locally signed C wrapper via a
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

The TCC grant is bound to the wrapper's CDHash. The wrapper therefore validates
every project file it loads before `exec`: `helper.py`, `send_gate.py`, and the
native confirmation helper must be regular files, have the expected owner, and
not be group/world writable. It rejects symlinks. The confirmation helper must
also be executable.

The **standard installer** keeps code in the user-owned bridge. A same-user
process can replace a file with another file that still has the expected user
owner and permissions, so this mode remains vulnerable to code replacement. A
tampered helper could:

- Bypass the v0.4.0 helper-side send gate — a replacement simply
  wouldn't check nonces.
- Exfiltrate message content via any channel the attacker chose:
  stdout, an HTTP POST, a file they read later.
- Silently defeat any future read-path authentication (HMAC or
  otherwise), because authentication is enforced by the helper, and
  the helper has been replaced.

The **hardened installer** puts code under root-owned
`/Library/Application Support/GrokBotIMessage/users/<uid>/libexec` and compiles the wrapper
to require UID 0 ownership for all loaded code. Runtime queues remain
user-owned. This prevents an ordinary same-user process from replacing trusted
code, although administrator/root compromise remains out of scope.

The FDA-bearing Python process opens the bridge path component-by-component
with `O_NOFOLLOW`, then performs request, response, log, and nonce operations
relative to verified directory descriptors. The bridge and runtime directories
must be owned by the current user with no group/world permissions. Existing
unsafe objects are rejected rather than chmodded. Request files must be regular
and current-user-owned; nonce files must additionally be mode `600`. Request
payloads are capped at 64 KiB.
The LaunchAgent sends process stdout/stderr to `/dev/null`, leaving structured
helper diagnostics to the same descriptor-relative internal logger.

The Grok installation has its own LaunchAgent, plist, executable names, bridge,
and hardened code root. The sibling Claude Cowork helper can therefore run at
the same time without sharing request queues, policy files, responses, logs, or
nonces. Both helpers still rely on the same macOS Messages database and
Messages Automation surface.

### Automation → Messages (v0.3.0+)

Required to send. The first send triggers a one-time macOS prompt:
"grokbot-imessage-helper wants to control Messages." The grant lives
under System Settings → Privacy & Security → Automation →
grokbot-imessage-helper → Messages.

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
  - Write a read request into the bridge folder. In standard mode it can receive
    any non-blocked content; hardened mode limits results to the root-owned
    allowlist.
  - Issue a `send_preview` request, read its nonce, and issue the matching
    `send` request. This can reach the native confirmation dialog, but it
    cannot silently send: the user must still review the displayed
    recipient and message and click **Send**.

Read requests are not tied to an interactive user session. Hardened mode narrows
the maximum disclosure to explicitly allowlisted chats, but any same-user process
can request and consume data from those chats. Do not allowlist conversations
whose disclosure to another local process would be unacceptable.

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
   - Full message text in a scrollable, read-only view
5. Cancel is the keyboard default. You must deliberately select **Send** to proceed. Clicking **Cancel** or waiting
   60 seconds aborts the send.
6. Only after the dialog is confirmed does the helper call `osascript`
   to send.

This two-layer gate is enforced **helper-side**. A process that writes
directly to the bridge folder and issues a `send` with no nonce, a forged
nonce, a replayed (already-consumed) nonce, an expired nonce (TTL is 60
seconds), or a nonce whose bound payload differs from the `send` request's
`(to, text, service)` is rejected before the dialog appears. Nonces are stored as per-file records under `<bridge-folder>/nonces/`; the nonce directory is mode `700` and nonce files are mode `600`. Nonces are single-use (deleted on consume) and are also deleted on validation failure so the same nonce cannot be retried with a corrected payload.

An attacker who can write to and read from the bridge can mint its own preview
nonce; the nonce is not an authorization boundary against that attacker. It
still prevents a blind one-request send, replay, or swapping the payload after a
preview. To complete any attacker-created send, the victim must deliberately
click **Send** in the native dialog showing the exact recipient, service, and
complete message body. Unexpected dialogs should always be cancelled.

The v0.3.x AI-side check still runs as well; the helper-side nonce gate
and native dialog are defense in depth, not a replacement.

**Behavior change for existing Cowork helpers:** The native dialog is new
in v1.0.0. Existing helpers from the sibling `claudecowork-imessage-skill`
repository (v0.4.0 and earlier) do not show the dialog. Users who upgrade
to this helper will experience the added confirmation step.

## Read policy

`contacts/blocked_chats.txt` is checked by the helper before any read
or send involving a listed identifier. The list is editable by the
user and is honored for both inbound (search/review) and outbound
(send) as of v0.3.0.

The standard-mode blocklist is best-effort. It is not a privacy boundary — anyone
who can edit the file can also remove entries, and the helper trusts
the list verbatim. Use it to prevent accidental exposure, not as a
security control.

Hardened mode bakes `allowlist` into the wrapper and validates a root-owned,
mode-600 allowlist under the same root-owned per-UID product tree.
The user receives read-only ACL access; changes go through `sudo` via
`configure_allowlist.py`. A missing, symlinked, writable, or non-root-owned file
causes the wrapper/helper to fail closed. The user-editable blocklist still takes
precedence.

## What leaves the machine

The helper itself does not make any outbound network connections. All
message content read from `chat.db` or sent via AppleScript is
processed on-device by the helper.

When Grok Bot uses this skill, message content that Grok Bot reads
passes through xAI's normal pipeline, which means it reaches
xAI's servers as part of the conversation, subject to
xAI's standard data-handling terms. If you don't want a specific
conversation touched, add the identifier to the blocklist or don't
invoke the skill on that range. Hardened users should allow only the minimum set
of conversations needed.

The plugin does **not**:

- Send telemetry.
- Phone home.
- Auto-update.
- Log message bodies or raw attributed-body bytes.

Response JSON can contain message content. Clients are required to delete each
response immediately after parsing it, and the helper reaps abandoned responses
after one hour. Response files and `control/log.txt` are mode `600`; parser logs
record only failure metadata and byte counts, never raw message bytes. Logs
rotate at 1 MiB with three backups.

Runtime paths are deliberately fail-closed. A symlink, FIFO, device, wrong
owner, or permissive directory causes that operation to be rejected; the helper
does not follow or repair the object. Run `tools/doctor.py` and rerun the chosen
installer to restore an expected directory layout.

## The chat.db copy

The helper uses SQLite's online backup API to create a consistent per-request
snapshot while Messages may still be writing to `chat.db`. It reads the
mode-600 snapshot and deletes it at the end of the request. An abnormal exit
(OOM or SIGKILL) can leave a stale copy behind.

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

- All source is in this repository. Read `bin/helper.py` and
  `bin/send_gate.py`; they run inside the FDA-granted Python process. The
  native confirmation source is `bin/confirm_imessage_send.m`.
- The C wrapper source is in `bin/cowork_imessage_helper.c`. It does
  approximately nothing — it exists to stabilize the CDHash. You can
  rebuild it yourself; the README documents the one-line `clang` command
  used by `install.sh`.
- Verify the repository contents match a tagged GitHub release. Release source
  archives include a `SHA256SUMS` file, and the source is small enough to diff
  against a local clone.
- After install, verify the LaunchAgent plist under
  `~/Library/LaunchAgents/com.jeffhuber.grokbot-imessage.plist` points only
  at the selected wrapper: the bridge-folder binary in standard mode or the
  root-owned code-root binary in hardened mode. It must carry no other
  arguments.

## Revoking

To fully remove the helper's access:

1. Run `./uninstall.sh` in the bridge folder.
2. Delete the bridge folder: `rm -rf ~/imessage-bridge` (or wherever
   you installed it).
3. System Settings → Privacy & Security → Full Disk Access → remove
   `grokbot-imessage-helper`.
4. System Settings → Privacy & Security → Automation →
   grokbot-imessage-helper → turn Messages off (or remove the entry
   entirely).

## Reporting a vulnerability

If you find a security issue, please do NOT open a public GitHub
issue. Email <jhuber+grokbotimessage@gmail.com> with details and, if possible, a
minimal reproduction. I will acknowledge within a few days and
coordinate disclosure.
