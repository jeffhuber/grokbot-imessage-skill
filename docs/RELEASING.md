# Releasing

1. Update `HELPER_VERSION` in `bin/helper.py`, `version` in `SKILL.md`, and
   `CHANGELOG.md` in the same pull request.
2. Run `python3 tools/check_version.py vX.Y.Z` and the checks in `README.md`.
3. Merge the release preparation commit to `main`.
4. Create and push an annotated tag: `git tag -s vX.Y.Z -m "vX.Y.Z"` and
   `git push origin vX.Y.Z`. Use `git tag -a` only when no signing key exists.
5. The Release workflow reruns the macOS checks and publishes `.tar.gz` and
   `.zip` source archives plus `SHA256SUMS`.

The workflow deliberately publishes source, not prebuilt FDA binaries. Users
compile and ad-hoc sign locally, so the reviewed source and granted binary stay
linked to their own installation path.
