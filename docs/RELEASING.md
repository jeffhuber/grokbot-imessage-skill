# Releasing

1. Update `HELPER_VERSION` in `bin/helper.py`, `version` in `SKILL.md`, and
   `CHANGELOG.md` in the same pull request.
2. Run `./tools/test.sh vX.Y.Z` and the native checks in CI. The script prints
   the exact validated interpreter it uses; set `IMESSAGE_TEST_PYTHON` to an
   absolute path to exercise another supported interpreter.
3. Merge the release preparation commit to `main`.
4. Create and push a signed annotated tag: `git tag -s vX.Y.Z -m "vX.Y.Z"` and
   `git push origin vX.Y.Z`. GitHub must report the tag signature as verified.
5. The Release workflow reruns the macOS checks, creates a draft release,
   uploads `.tar.gz` and `.zip` source archives plus `SHA256SUMS`, then
   publishes. Repository release immutability must be enabled before the tag is
   pushed.
6. Download `SHA256SUMS` and every asset from the published release, verify the
   checksums, and record a successful standard-install smoke test.

The workflow deliberately publishes source, not prebuilt FDA binaries. Users
compile and ad-hoc sign locally, so the reviewed source and granted binary stay
linked to their own installation path.
