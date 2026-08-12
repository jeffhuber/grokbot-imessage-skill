#!/bin/bash
# uninstall.sh — remove the Grok Bot iMessage launchd agent.
#
# Leaves files under bin/, control/, and contacts/ in place so any captured
# data is preserved. Also leaves the FDA grant — you must remove that
# manually in System Settings -> Privacy & Security -> Full Disk Access.

set -euo pipefail

LABEL="com.user.cowork-imessage"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$UID/$LABEL" || true
    echo "  launchd agent unloaded"
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
EOF
