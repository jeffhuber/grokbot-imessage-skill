# Changelog

Notable changes are documented here. Releases use semantic versioning for the
helper and skill, while the protocol has its own major/minor compatibility
version reported by the `status` action.

## 1.1.0 - Unreleased

- Add native, fail-closed full-message confirmation before every send.
- Add atomic request, response, and nonce handling with bounded data retention.
- Add SQLite online snapshots, diagnostics, skill installation, tests, and CI.
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
