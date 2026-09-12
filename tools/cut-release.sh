#!/usr/bin/env bash
# Cut a signed release tag for grokbot-imessage-skill.
#
# Usage: ./tools/cut-release.sh vX.Y.Z
#
# This script:
# - Verifies that tag.gpgsign is enabled (local or global config)
# - Creates a signed annotated tag locally
# - Does NOT push the tag (you must push manually after review)
#
# The Release workflow requires a verified signed annotated tag before
# publishing. Do not weaken or bypass the signature check.

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 vX.Y.Z" >&2
  exit 1
fi

TAG="$1"

if [[ ! "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Error: Tag must match vX.Y.Z format (e.g., v1.4.2)" >&2
  exit 1
fi

# Check if tag.gpgsign is enabled (local or global)
GPGSIGN_ENABLED="$(git config --get tag.gpgsign || echo "false")"
if [[ "$GPGSIGN_ENABLED" != "true" ]]; then
  echo "Error: tag.gpgsign is not enabled." >&2
  echo "Run: git config --global tag.gpgsign true" >&2
  echo "Or:  git config tag.gpgsign true (for this repo only)" >&2
  exit 1
fi

# Check if the tag already exists
if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "Error: Tag $TAG already exists" >&2
  exit 1
fi

# Create the signed annotated tag
echo "Creating signed annotated tag: $TAG"
git tag -s "$TAG" -m "$TAG"

echo "✓ Tag $TAG created and signed locally"
echo ""
echo "Review the tag, then push it:"
echo "  git push origin $TAG"
echo ""
echo "After pushing, the Release workflow will verify the signature,"
echo "run CI checks, and publish the release."
