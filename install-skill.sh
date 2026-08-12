#!/bin/bash
# Install this repository's Grok skill into Grok's user-level discovery path.

set -euo pipefail

if [[ "$EUID" -eq 0 ]]; then
    echo "Error: run install-skill.sh as your normal user, not with sudo." >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GROK_SKILLS_ROOT="${GROK_HOME:-$HOME/.grok}/skills"
SKILL_DEST="$GROK_SKILLS_ROOT/imessage-grok-bot"

mkdir -p "$SKILL_DEST"
chmod 700 "$GROK_SKILLS_ROOT" "$SKILL_DEST"
cp "$REPO_ROOT/SKILL.md" "$SKILL_DEST/SKILL.md"
chmod 600 "$SKILL_DEST/SKILL.md"

echo "Installed Grok skill: $SKILL_DEST/SKILL.md"
if command -v grok >/dev/null 2>&1; then
    echo "Verifying Grok discovery with: grok inspect"
    if ! grok inspect 2>&1 | grep -q "imessage-grok-bot"; then
        echo "Warning: grok inspect did not report imessage-grok-bot." >&2
        echo "Open Grok and run /skills, or inspect $SKILL_DEST." >&2
        exit 1
    fi
    echo "Grok discovered imessage-grok-bot."
else
    echo "Grok CLI not found; run 'grok inspect' after installing Grok."
fi
