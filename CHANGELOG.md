# Changelog

Notable changes are documented here. Releases use semantic versioning for the
helper and skill, while the protocol has its own major/minor compatibility
version reported by the `status` action.

## Unreleased

## 1.4.7 - 2026-09-13

- Raised default snapshot size limit from 500 MB to 1024 MB to accommodate
  larger local chat.db files without manual override. The 500 MB ceiling was
  the main remaining UX complaint for users with large message histories
  (~600 MB databases). Override remains available via `IMESSAGE_SNAPSHOT_MAX_MB`
  environment variable for even larger databases.

## 1.4.6 - 2026-09-13

- Version bump for signed installable GitHub release with checksummed assets.
  Prior `v1.4.5` tag exists without release assets due to unsigned lightweight
  tag collision with immutable release policy.

## 1.4.4 - 2026-09-12

- Environment variable pass-through: `IMESSAGE_SNAPSHOT_MAX_MB` is now forwarded
  from the LaunchAgent's environment to the Python helper in both baked-path and
  product-build modes. Operators with large chat databases (>500 MB) can raise
  the in-memory snapshot limit by adding `IMESSAGE_SNAPSHOT_MAX_MB` to the
  `EnvironmentVariables` dict in the LaunchAgent plist. The default remains
  500 MB (fail-closed). Invalid values continue to fall back to the default.
- Documentation: Added LaunchAgent `EnvironmentVariables` configuration example
  to the plist template and troubleshooting sections in README.md and SECURITY.md,
  documenting how to set `IMESSAGE_SNAPSHOT_MAX_MB` for large databases.

## 1.4.3 - 2026-09-12

- Snapshot OOM hardening: Include `chat.db-wal` size in snapshot limit check.
  SQLite's backup API includes uncommitted WAL data in the in-memory snapshot,
  so both chat.db and chat.db-wal now count against `IMESSAGE_SNAPSHOT_MAX_MB`.
  Error messages show breakdown (db + wal bytes) when limit is exceeded.

## 1.4.2 - 2026-09-12

- Snapshot OOM hardening: Add size guard before in-memory `chat.db` snapshot.
  Default limit is 500 MB; override with `IMESSAGE_SNAPSHOT_MAX_MB` (integer
  megabytes). Databases exceeding the limit are rejected with a clear error
  message instead of causing OOM. Invalid or zero values fail closed at the
  default.
- Documentation: Expanded `docs/RELEASING.md` with GPG key generation, GitHub
  setup, and git tag signing configuration for maintainers preparing signed
  releases. Added optional `tools/cut-release.sh` script to streamline local
  tag creation with pre-flight checks.

## 1.4.1 - 2026-09-12

- Security hardening: Batch A (request/response atomicity, bounded retention,
  memory-only snapshots, send tempfile race fix, default-deny read allowlist),
  Batch B (nonce collision hardening, install-time validation, send-gate state
  isolation), and Batch C LOWs (enhanced diagnostics, pre-validation).
- Privacy: Scrubbed fictional contact examples from working tree documentation;
  commit message scrub completed 2026-09-13 to replace example contact names
  with fictional placeholders throughout repository history.
- Documentation: Clarified group chat ID matching uses exact case-insensitive
  comparison (not substring matching) for blocklist and allowlist enforcement.
- Repository cleanup: Removed history-rewrite inventory files after merge.

## 1.3.0 - 2026-08-16

- Bridge protocol 1.2: add the `list_chats` action, which enumerates threads
  with recent activity for policy discovery and never selects message bodies
  (a sentinel-body fixture asserts this); add worker-enforced bridge roles
  (`IMESSAGE_BRIDGE_ROLE`, default `host`) — `list_chats` is served only on a
  `manager` bridge, body-returning and send actions only on a `host` bridge,
  and unknown roles serve nothing; `status` reports `bridge_role` and
  `allowed_actions`. DIY installs are unaffected: nothing sets the role, so
  every existing action behaves as before.
- CORE-5a product-mode hardening: apply the same uid/mode ownership checks to
  `read_policy.txt` that are already enforced for `send_policy.txt` on wrapper
  startup, reject reads from host bridges when read policy is unavailable; treat
  root-owned files as satisfying `require_uid_owner` checks; add defense-in-depth
  check to `_load_send_gate()` when loading product-mode send-gate state; harden
  env-plumbing re-import test to use `os.path.realpath()`.
- CORE-5b role-gating extensions: introduce `send_policy.txt` support for manager
  bridges, allow manager bridges to operate without nonce files.
- CORE-8 product-mode send policy: enforce send policy validation in product mode,
  hide launchd label from product-mode environments, add per-bridge file lock parity.

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
