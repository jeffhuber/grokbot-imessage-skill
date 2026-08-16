# Changelog

Notable changes are documented here. Releases use semantic versioning for the
helper and skill, while the protocol has its own major/minor compatibility
version reported by the `status` action.

## Unreleased

- Bridge protocol 1.2: add the `list_chats` action, which enumerates threads
  with recent activity for policy discovery and never selects message bodies
  (a sentinel-body fixture asserts this); add worker-enforced bridge roles
  (`IMESSAGE_BRIDGE_ROLE`, default `host`) — `list_chats` is served only on a
  `manager` bridge, body-returning and send actions only on a `host` bridge,
  and unknown roles serve nothing; `status` reports `bridge_role` and
  `allowed_actions`. DIY installs are unaffected: nothing sets the role, so
  every existing action behaves as before.

## 1.2.2 - 2026-08-14

- Harden hardened-install Python selection by validating interpreter ownership
  and path permissions before executing a compatibility probe.
- Report shell-only `chat.db` access accurately in `doctor.py`; the wrapper's Full
  Disk Access remains verified by the smoke test.
- Pin setup-python by commit and prevent release checkout credential persistence.
- Add a supported-interpreter test entrypoint and announcement-ready install,
  upgrade, release-integrity, and host-support documentation.
- Require a GitHub-verified signed tag and draft-first publication for immutable
  release assets.

## 1.2.1 - 2026-08-13

- Select and validate one supported Python interpreter for installer tasks and
  the FDA wrapper, even when an older `python3` appears first on `PATH`.
- Fail closed on an invalid `IMESSAGE_PYTHON`; hardened installs require a
  root-owned interpreter path that another user process cannot replace.
- Validate one deterministic ISO-dated changelog heading during release checks.

## 1.2.0 - 2026-08-13

- Adopt shared-core manifest for deterministic cross-repo comparison with Claude
  Cowork and ChatGPT/Codex siblings. Add per-repository CI validation and a daily
  scheduled parity workflow.
- Fail closed when `IMESSAGE_BRIDGE_DIR` is unset or empty; the retired default
  path is no longer available.
- README now leads with standard `./install.sh` as the primary install path;
  hardened mode is presented as optional defense-in-depth for high-risk scenarios.
- Protocol version remains 1.1 (backward-compatible with 1.1.0 and 1.1.1).

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
