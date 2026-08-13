# Changelog

Notable changes are documented here. Releases use semantic versioning for the
helper and skill, while the protocol has its own major/minor compatibility
version reported by the `status` action.

## Unreleased

## 1.1.1 - 2026-08-12

- Rename wrapper source from `bin/cowork_imessage_helper.c` to `bin/imessage_helper.c`.
- Export `IMESSAGE_BRIDGE_DIR` (new preferred name) and keep `COWORK_IMESSAGE_BRIDGE_DIR` as a one-release alias.
- Refuse the retired `~/cowork-imessage` send-gate default; `IMESSAGE_BRIDGE_DIR` is now required.
- Document three-host coexistence with distinct LaunchAgents, wrappers, and bridge folders.
- README now leads with standard `./install.sh` as the primary install path; hardened mode is presented as optional defense-in-depth.
- Add native send-dialog screenshot to documentation.

## 1.1.0 - 2026-08-12

- Add native, fail-closed full-message confirmation before every send.
- Add atomic request, response, and nonce handling with bounded data retention.
- Add SQLite online snapshots, diagnostics, skill installation, tests, and CI.
- Open completed Messages snapshots as immutable, read-only databases so
  WAL-marked snapshots do not require writable `-wal` or `-shm` sidecars.
- Add protocol and helper version reporting.
- Add an optional hardened install with root-owned executable code, wrapper
  validation of every loaded component, and a root-owned default-deny read
  allowlist.
- Separate trusted code paths from user-owned request, response, log, and nonce
  state.
- Anchor runtime filesystem operations to no-follow directory descriptors so
  the FDA-bearing helper rejects symlinked or permissive bridge paths.
- Reject oversized or structurally invalid request JSON without interrupting
  later requests in the queue.
- Give Grok Bot its own LaunchAgent, plist, wrapper, and confirmation identity
  so it can coexist with the Claude Cowork sibling helper.
- Migrate the old shared LaunchAgent only when it points to this exact Grok
  installation.

## 1.0.0 - 2026-08-11

- Initial public Grok Bot iMessage skill.
