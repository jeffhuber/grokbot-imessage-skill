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
    inspect_output=""
    inspect_status=0
    inspect_output="$(grok inspect 2>&1)" || inspect_status=$?
    if [[ "$inspect_status" -eq 0 ]] && grep -q "imessage-grok-bot" <<<"$inspect_output"; then
        echo "Grok discovered imessage-grok-bot."
    else
        echo "Warning: grok inspect did not report imessage-grok-bot." >&2
        if [[ "$inspect_status" -ne 0 ]]; then
            echo "grok inspect exited with status $inspect_status." >&2
        fi
        echo "Reload Grok and run /skills, or inspect $SKILL_DEST." >&2
        echo "The skill file was installed; discovery can lag until Grok reloads." >&2
    fi
else
    echo "Grok CLI not found; run 'grok inspect' after installing Grok."
fi
