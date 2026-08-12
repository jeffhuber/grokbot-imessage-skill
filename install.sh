#!/bin/bash
# install.sh — one-time setup for the Grok Bot iMessage helper.
#
# What this does, in order:
#   1. Sanity-checks that we're on macOS with the Xcode Command Line Tools
#      installed (for clang + codesign).
#   2. Creates control/requests, control/responses, and contacts/ if missing.
#   3. chmod 500 bin/helper.py so the wrapper won't refuse to exec it.
#   4. Compiles bin/cowork-imessage-helper with the install dir baked in.
#   5. Ad-hoc code-signs the wrapper so macOS can give FDA a stable identity
#      to attach to. Re-signing on content-identical rebuilds keeps the grant.
#   6. Fills in the launchd plist template and installs it under
#      ~/Library/LaunchAgents/com.user.cowork-imessage.plist, then bootstraps it.
#   7. Prints exact next-steps: grant Full Disk Access to the wrapper binary.
#
# Safe to re-run. It will not clobber grants or overwrite user files.

set -euo pipefail

# ---- Early guard: reject root/sudo ------------------------------------------
if [[ "$EUID" -eq 0 ]]; then
    printf "\033[31mError: Do not run this installer as root or with sudo.\033[0m\n" 1>&2
    printf "This is a per-user LaunchAgent. Run as your normal user:\n" 1>&2
    printf "  ./install.sh\n" 1>&2
    exit 1
fi

INSTALL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$INSTALL_ROOT/bin"
CONTROL_DIR="$INSTALL_ROOT/control"
CONTACTS_DIR="$INSTALL_ROOT/contacts"
HELPER_PY="$BIN_DIR/helper.py"
WRAPPER_SRC="$BIN_DIR/cowork_imessage_helper.c"
WRAPPER_BIN="$BIN_DIR/cowork-imessage-helper"
PLIST_TEMPLATE="$INSTALL_ROOT/com.user.cowork-imessage.plist.template"
PLIST_DEST="$HOME/Library/LaunchAgents/com.user.cowork-imessage.plist"
LAUNCHCTL_LABEL="com.user.cowork-imessage"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
red() { printf "\033[31m%s\033[0m\n" "$*" 1>&2; }

# ---- 1. sanity checks ----------------------------------------------------
if [[ "$(uname)" != "Darwin" ]]; then
    red "This installer only runs on macOS."
    exit 1
fi

if ! xcode-select -p >/dev/null 2>&1; then
    red "Xcode Command Line Tools are required to build the wrapper."
    red "Install them with: xcode-select --install"
    exit 1
fi

for cmd in clang codesign launchctl python3; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        red "Required command not found: $cmd"
        exit 1
    fi
done

if [[ ! -f "$WRAPPER_SRC" ]]; then
    red "Missing $WRAPPER_SRC"
    exit 1
fi
if [[ ! -f "$HELPER_PY" ]]; then
    red "Missing $HELPER_PY"
    exit 1
fi
if [[ ! -f "$PLIST_TEMPLATE" ]]; then
    red "Missing $PLIST_TEMPLATE"
    exit 1
fi

bold "Grok Bot iMessage helper installer"
echo "  install root : $INSTALL_ROOT"
echo "  helper.py    : $HELPER_PY"
echo "  wrapper bin  : $WRAPPER_BIN"
echo "  launchd plist: $PLIST_DEST"
echo

# ---- 2. control / contacts directories -----------------------------------
mkdir -p "$CONTROL_DIR/requests" "$CONTROL_DIR/responses" "$CONTACTS_DIR"
touch "$CONTROL_DIR/log.txt"
chmod 700 "$CONTROL_DIR" "$CONTROL_DIR/requests" "$CONTROL_DIR/responses" "$CONTACTS_DIR"
chmod 600 "$CONTROL_DIR/log.txt"

if [[ ! -f "$CONTACTS_DIR/blocked_chats.txt" ]]; then
    cat > "$CONTACTS_DIR/blocked_chats.txt" <<'EOF'
# Blocked chats. One entry per line. Lines starting with # are ignored.
# Matches:
#   - phone numbers: last 10 digits are compared (e.g. +1-555-123-4567,
#     5551234567, and (555) 123-4567 all match the same chat).
#   - email addresses: full case-insensitive match.
#   - group ids / substrings: anything starting with "chat" or containing
#     a distinctive substring.
#
# Messages from blocked chats are dropped BEFORE the response JSON is
# written, so their text never enters the agent's context.
#
# Examples (remove the # to activate):
# +15551234567
# therapist@example.com
# chat123456789
EOF
    chmod 600 "$CONTACTS_DIR/blocked_chats.txt"
    green "  created $CONTACTS_DIR/blocked_chats.txt (empty)"
fi

# ---- 3. lock down helper.py ----------------------------------------------
chmod 500 "$HELPER_PY"
green "  chmod 500 $HELPER_PY"

# ---- 4. build wrapper binary --------------------------------------------
bold "Building wrapper binary..."
PYTHON3_PATH="$(command -v python3)"
# Prefer the stable system path if available; it's a more predictable FDA target.
if [[ -x /usr/bin/python3 ]]; then
    PYTHON3_PATH="/usr/bin/python3"
fi

clang -Wall -Wextra -Werror -O2 \
    -DHELPER_SCRIPT="\"$HELPER_PY\"" \
    -DPYTHON_INTERPRETER="\"$PYTHON3_PATH\"" \
    -o "$WRAPPER_BIN" "$WRAPPER_SRC"
chmod 700 "$WRAPPER_BIN"
green "  built $WRAPPER_BIN"

# ---- 5. ad-hoc code-sign -------------------------------------------------
# The hardened runtime flag blocks DYLD_INSERT_LIBRARIES et al, so an
# attacker can't hijack our FDA grant via library injection.
codesign --force --sign - --options runtime "$WRAPPER_BIN"
green "  ad-hoc signed $WRAPPER_BIN"

# Record the CDHash so the user can tell whether a re-sign is needed later.
CDHASH=$(codesign -dvvv "$WRAPPER_BIN" 2>&1 | awk -F'=' '/CDHash=/{print $2; exit}')
echo "  cdhash: ${CDHASH:-unknown}"

# ---- 6. launchd plist ----------------------------------------------------
mkdir -p "$(dirname "$PLIST_DEST")"

# Use Python to generate the plist with proper XML escaping instead of sed.
python3 - "$INSTALL_ROOT" "$PLIST_DEST" "$PLIST_TEMPLATE" <<'PYGEN'
import sys, xml.etree.ElementTree as ET

install_root, dest, template = sys.argv[1], sys.argv[2], sys.argv[3]

# Read template and replace placeholder with properly escaped path.
tree = ET.parse(template)
root = tree.getroot()

# Find all <string> elements and replace the placeholder.
for elem in root.iter("string"):
    if elem.text and "{{INSTALL_ROOT}}" in elem.text:
        elem.text = elem.text.replace("{{INSTALL_ROOT}}", install_root)

tree.write(dest, encoding="UTF-8", xml_declaration=True)
PYGEN

chmod 644 "$PLIST_DEST"
green "  wrote $PLIST_DEST"

# Bootstrap (or restart) the agent.
if launchctl print "gui/$UID/$LAUNCHCTL_LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$UID/$LAUNCHCTL_LABEL"
    echo "  existing agent unloaded"
fi
launchctl bootstrap "gui/$UID" "$PLIST_DEST"
launchctl enable "gui/$UID/$LAUNCHCTL_LABEL"
green "  launchd agent bootstrapped ($LAUNCHCTL_LABEL)"

# ---- 7. finish ------------------------------------------------------------
echo
bold "Install complete."
echo
yellow "ONE MANUAL STEP REMAINS: grant Full Disk Access to the wrapper."
echo
echo "  1. Open: System Settings -> Privacy & Security -> Full Disk Access"
echo "  2. Click the + button, then press Cmd-Shift-G and paste:"
echo
echo "       $WRAPPER_BIN"
echo
echo "  3. Select 'cowork-imessage-helper' and make sure its toggle is ON."
echo "  4. (If prompted to quit and reopen anything, just click 'Later'.)"
echo
echo "Verify by asking Grok Bot: \"review my imessages over the last 2 days\""
echo
yellow "Note on sending (v0.3.0+):"
echo "  The first time you ask Grok Bot to send an iMessage, macOS will"
echo "  prompt 'cowork-imessage-helper wants to control Messages.' Click OK."
echo "  After that, the grant lives under:"
echo "    System Settings -> Privacy & Security -> Automation"
echo "  (This is a separate permission from Full Disk Access.)"
echo
echo "Logs: $CONTROL_DIR/log.txt"
echo "Uninstall: ./uninstall.sh"
