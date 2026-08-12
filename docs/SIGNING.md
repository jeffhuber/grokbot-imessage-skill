# Signing and Notarization

This project currently publishes source archives and checksums, not prebuilt
macOS executables. Each installer compiles binaries with paths specific to that
Mac, then signs them locally:

- Standard and hardened installs use an ad hoc signature by default. This gives
  TCC a stable local CDHash but does not establish publisher identity.
- `CODESIGN_IDENTITY="Developer ID Application: ..." ./install-hardened.sh`
  uses a local Developer ID identity and a trusted timestamp when available.

The resulting binaries are **not notarized by this project**. Notarization is an
Apple review of a distributable artifact; it cannot be truthfully inferred from
an ad hoc signature or a locally compiled, path-baked binary. `codesign --verify`
checks structural validity only.

A future notarized distribution should use a generic, versioned package, keep
credentials in protected CI secrets, submit with `notarytool`, staple the ticket,
publish signature/notarization verification commands, and retain the source
archive and reproducible build inputs for that exact version. Until that exists,
review the source, verify `SHA256SUMS`, and use the root-owned hardened install
when its trust model fits your machine.
