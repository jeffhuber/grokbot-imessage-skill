# Releasing

## Prerequisites

Releases require a GPG-signed annotated tag. The Release workflow verifies the
tag signature before building and publishing artifacts. If you haven't set up
GPG signing yet, follow the setup instructions below.

### GPG Key Setup (one-time)

If you don't already have a GPG key configured for git tag signing:

1. **Generate a GPG key:**

   ```bash
   gpg --full-generate-key
   ```

   - Choose `RSA and RSA` (option 1)
   - Key size: `4096` bits
   - Expiration: `0` (does not expire) or a long expiration (e.g., 5 years)
   - Enter your name and the email address associated with your GitHub account
   - Choose a strong passphrase

2. **List your GPG keys and note the key ID:**

   ```bash
   gpg --list-secret-keys --keyid-format=long
   ```

   The key ID is the 16-character hex string after `rsa4096/`:

   ```
   sec   rsa4096/ABCD1234EFGH5678 2026-09-12 [SC]
   ```

   In this example, `ABCD1234EFGH5678` is your key ID.

3. **Add your GPG key to GitHub:**

   Export your public key:

   ```bash
   gpg --armor --export ABCD1234EFGH5678
   ```

   Copy the output (including `-----BEGIN PGP PUBLIC KEY BLOCK-----` and
   `-----END PGP PUBLIC KEY BLOCK-----`).

   Go to [GitHub Settings → SSH and GPG keys](https://github.com/settings/keys),
   click **New GPG key**, paste your public key, and save.

4. **Configure git to sign tags with your GPG key:**

   ```bash
   git config --global user.signingkey ABCD1234EFGH5678
   git config --global tag.gpgsign true
   ```

   The second command makes tag signing automatic. Omit `--global` to configure
   only this repository.

5. **Verify your setup:**

   Create a test tag:

   ```bash
   git tag -s test-tag -m "Test signed tag"
   ```

   If GPG prompts for your passphrase and the tag is created, signing is working.
   Delete the test tag:

   ```bash
   git tag -d test-tag
   ```

GitHub will now show a "Verified" badge on tags signed with your registered GPG key.

### Optional: Automated Release Script

The repository includes `tools/cut-release.sh` (optional) to streamline tag
creation. It checks that `tag.gpgsign` is enabled and creates a signed annotated
tag locally without pushing. Review the tag, then push manually:

```bash
./tools/cut-release.sh vX.Y.Z
git push origin vX.Y.Z
```

The script does NOT push tags or weaken any verification—it's a convenience
wrapper around `git tag -s` with pre-flight checks.

## Release Process

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
