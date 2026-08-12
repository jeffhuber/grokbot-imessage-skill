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

## 1.0.0 - 2026-08-11

- Initial public Grok Bot iMessage skill.
