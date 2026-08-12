#!/bin/bash
# uninstall.sh — remove the Grok Bot iMessage launchd agent.
#
# Leaves files under bin/, control/, and contacts/ in place so any captured
# data is preserved. Also leaves the FDA grant — you must remove that
# manually in System Settings -> Privacy & Security -> Full Disk Access.

set -euo pipefail

# ---- Early guard: reject root/sudo ------------------------------------------
if [[ "$EUID" -eq 0 ]]; then
    printf "\033[31mError: Do not run this uninstaller as root or with sudo.\033[0m\n" 1>&2
    printf "This is a per-user LaunchAgent. Run as your normal user:\n" 1>&2
    printf "  ./uninstall.sh\n" 1>&2
    exit 1
fi

LABEL="com.user.cowork-imessage"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
SKILL_DEST="${GROK_HOME:-$HOME/.grok}/skills/imessage-grok-bot"

if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$UID/$LABEL"
    echo "  launchd agent unloaded"
fi

if [[ -d "$SKILL_DEST" ]]; then
    rm -rf "$SKILL_DEST"
    echo "  removed Grok skill $SKILL_DEST"
fi

if [[ -f "$PLIST_DEST" ]]; then
    rm -f "$PLIST_DEST"
    echo "  removed $PLIST_DEST"
fi

cat <<EOF

Uninstalled the launchd agent.

To fully remove the helper:
  - Delete this folder.
  - Open System Settings -> Privacy & Security -> Full Disk Access and
    revoke 'cowork-imessage-helper'.
  - Open System Settings -> Privacy & Security -> Automation and revoke
    'cowork-imessage-helper -> Messages'.
EOF
