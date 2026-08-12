#!/bin/bash
# Install trusted code root-owned and keep request/response state user-owned.

set -euo pipefail
ORIGINAL_PATH="$PATH"
PATH="/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

if [[ "$EUID" -eq 0 ]]; then
    echo "Error: run this script as your normal user; it invokes sudo narrowly." >&2
    exit 1
fi
if [[ "$(uname)" != "Darwin" ]]; then
    echo "Error: the hardened installer only runs on macOS." >&2
    exit 1
fi

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRODUCT_ROOT="/Library/Application Support/GrokBotIMessage"
USER_ROOT="$PRODUCT_ROOT/users/$UID"
CODE_ROOT="$USER_ROOT/libexec"
CONFIG_ROOT="$USER_ROOT/config"
BRIDGE_ROOT="${GROKBOT_IMESSAGE_BRIDGE:-$HOME/Library/Application Support/GrokBotIMessage}"
PLIST_TEMPLATE="$SOURCE_ROOT/com.user.cowork-imessage.plist.template"
PLIST_DEST="$HOME/Library/LaunchAgents/com.user.cowork-imessage.plist"
LABEL="com.user.cowork-imessage"
ALLOWLIST="$CONFIG_ROOT/allowed_chats.txt"
CURRENT_USER="$(id -un)"
BUILD_DIR="$(mktemp -d -t grokbot-imessage-build.XXXXXX)"
trap 'rm -rf "$BUILD_DIR"' EXIT

for cmd in clang codesign launchctl python3 sudo; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "Error: required command not found: $cmd" >&2
        exit 1
    fi
done
if ! xcode-select -p >/dev/null 2>&1; then
    echo "Error: install Xcode Command Line Tools with xcode-select --install" >&2
    exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 9))'; then
    echo "Error: Python 3.9 or newer is required." >&2
    exit 1
fi

for path in \
    "$SOURCE_ROOT/bin/helper.py" \
    "$SOURCE_ROOT/bin/send_gate.py" \
    "$SOURCE_ROOT/bin/cowork_imessage_helper.c" \
    "$SOURCE_ROOT/bin/confirm_imessage_send.m" \
    "$SOURCE_ROOT/tools/doctor.py" \
    "$SOURCE_ROOT/tools/configure_allowlist.py" \
    "$SOURCE_ROOT/contacts/blocked_chats.txt.template" \
    "$SOURCE_ROOT/contacts/allowed_chats.txt.template" \
    "$SOURCE_ROOT/install-skill.sh" \
    "$PLIST_TEMPLATE"; do
    if [[ ! -f "$path" ]]; then
        echo "Error: missing source file: $path" >&2
        exit 1
    fi
done

mkdir -p "$BRIDGE_ROOT/control/requests" "$BRIDGE_ROOT/control/responses" \
    "$BRIDGE_ROOT/contacts"
BRIDGE_ROOT="$(cd "$BRIDGE_ROOT" && pwd -P)"
touch "$BRIDGE_ROOT/control/log.txt"
chmod 700 "$BRIDGE_ROOT" "$BRIDGE_ROOT/control" \
    "$BRIDGE_ROOT/control/requests" "$BRIDGE_ROOT/control/responses" \
    "$BRIDGE_ROOT/contacts"
chmod 600 "$BRIDGE_ROOT/control/log.txt"

if [[ ! -f "$BRIDGE_ROOT/contacts/blocked_chats.txt" ]]; then
    cp "$SOURCE_ROOT/contacts/blocked_chats.txt.template" \
        "$BRIDGE_ROOT/contacts/blocked_chats.txt"
fi
printf 'allowlist\n' > "$BRIDGE_ROOT/contacts/read_policy.txt"
chmod 600 "$BRIDGE_ROOT/contacts/blocked_chats.txt" \
    "$BRIDGE_ROOT/contacts/read_policy.txt"

echo "Requesting administrator access for the root-owned code and policy..."
sudo -v
sudo /usr/bin/install -d -o root -g wheel -m 755 \
    "$PRODUCT_ROOT" "$PRODUCT_ROOT/users" "$USER_ROOT" "$CODE_ROOT" \
    "$CODE_ROOT/bin" "$CODE_ROOT/tools" "$CONFIG_ROOT"
if [[ -L "$ALLOWLIST" ]]; then
    echo "Error: hardened allowlist must not be a symlink: $ALLOWLIST" >&2
    exit 1
fi
if [[ ! -e "$ALLOWLIST" ]]; then
    sudo /usr/bin/install -o root -g wheel -m 600 \
        "$SOURCE_ROOT/contacts/allowed_chats.txt.template" "$ALLOWLIST"
fi
if ! python3 - "$ALLOWLIST" <<'PYCHECK'; then
import os
import stat
import sys

metadata = os.lstat(sys.argv[1])
valid = stat.S_ISREG(metadata.st_mode) and metadata.st_uid == 0
valid = valid and not metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
raise SystemExit(0 if valid else 1)
PYCHECK
    echo "Error: existing hardened allowlist is not a protected root-owned file." >&2
    exit 1
fi
if ! sudo /bin/chmod -N "$ALLOWLIST" 2>/dev/null; then
    echo "  no existing ACL to clear"
fi
sudo /bin/chmod +a "user:$CURRENT_USER allow read" "$ALLOWLIST"

PYTHON3_PATH="$(command -v python3)"
if [[ -x /usr/bin/python3 ]]; then
    PYTHON3_PATH="/usr/bin/python3"
fi

clang -Wall -Wextra -Werror -fobjc-arc \
    -framework AppKit -framework Foundation \
    -o "$BUILD_DIR/confirm-imessage-send" "$SOURCE_ROOT/bin/confirm_imessage_send.m"

clang -Wall -Wextra -Werror -O2 \
    -DHELPER_SCRIPT="\"$CODE_ROOT/bin/helper.py\"" \
    -DSEND_GATE_SCRIPT="\"$CODE_ROOT/bin/send_gate.py\"" \
    -DCONFIRM_HELPER="\"$CODE_ROOT/bin/confirm-imessage-send\"" \
    -DBRIDGE_ROOT="\"$BRIDGE_ROOT\"" \
    -DPYTHON_INTERPRETER="\"$PYTHON3_PATH\"" \
    -DEXPECTED_CODE_UID=0 \
    -DREAD_POLICY_MODE='"allowlist"' \
    -DREAD_ALLOWLIST_PATH="\"$ALLOWLIST\"" \
    -DREQUIRE_ROOT_POLICY=1 \
    -o "$BUILD_DIR/cowork-imessage-helper" \
    "$SOURCE_ROOT/bin/cowork_imessage_helper.c"

CODESIGN_IDENTITY="${CODESIGN_IDENTITY:--}"
SIGN_ARGS=(--force --sign "$CODESIGN_IDENTITY" --options runtime)
if [[ "$CODESIGN_IDENTITY" != "-" ]]; then
    SIGN_ARGS+=(--timestamp)
fi
codesign "${SIGN_ARGS[@]}" "$BUILD_DIR/cowork-imessage-helper"
codesign "${SIGN_ARGS[@]}" "$BUILD_DIR/confirm-imessage-send"

sudo /usr/bin/install -o root -g wheel -m 444 \
    "$SOURCE_ROOT/bin/helper.py" "$CODE_ROOT/bin/helper.py"
sudo /usr/bin/install -o root -g wheel -m 444 \
    "$SOURCE_ROOT/bin/send_gate.py" "$CODE_ROOT/bin/send_gate.py"
sudo /usr/bin/install -o root -g wheel -m 444 \
    "$SOURCE_ROOT/bin/cowork_imessage_helper.c" "$CODE_ROOT/bin/cowork_imessage_helper.c"
sudo /usr/bin/install -o root -g wheel -m 444 \
    "$SOURCE_ROOT/bin/confirm_imessage_send.m" "$CODE_ROOT/bin/confirm_imessage_send.m"
sudo /usr/bin/install -o root -g wheel -m 555 \
    "$BUILD_DIR/cowork-imessage-helper" "$CODE_ROOT/bin/cowork-imessage-helper"
sudo /usr/bin/install -o root -g wheel -m 555 \
    "$BUILD_DIR/confirm-imessage-send" "$CODE_ROOT/bin/confirm-imessage-send"
sudo /usr/bin/install -o root -g wheel -m 555 \
    "$SOURCE_ROOT/tools/doctor.py" "$CODE_ROOT/tools/doctor.py"
sudo /usr/bin/install -o root -g wheel -m 555 \
    "$SOURCE_ROOT/tools/configure_allowlist.py" "$CODE_ROOT/tools/configure_allowlist.py"

mkdir -p "$(dirname "$PLIST_DEST")"
python3 - "$CODE_ROOT" "$BRIDGE_ROOT" "$PLIST_DEST" "$PLIST_TEMPLATE" <<'PYGEN'
import sys
import xml.etree.ElementTree as ET

code_root, bridge_root, destination, template = sys.argv[1:]
tree = ET.parse(template)
for element in tree.getroot().iter("string"):
    if element.text:
        element.text = element.text.replace("{{CODE_ROOT}}", code_root)
        element.text = element.text.replace("{{BRIDGE_ROOT}}", bridge_root)
tree.write(destination, encoding="UTF-8", xml_declaration=True)
PYGEN
chmod 644 "$PLIST_DEST"

if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$UID/$LABEL"
fi
launchctl bootstrap "gui/$UID" "$PLIST_DEST"
launchctl enable "gui/$UID/$LABEL"
PATH="$ORIGINAL_PATH" "$SOURCE_ROOT/install-skill.sh"

cat <<EOF

Hardened install complete.

Trusted code (root-owned): $CODE_ROOT
Runtime bridge (user-owned): $BRIDGE_ROOT
Read policy: root-owned allowlist (default-deny)

Add an allowed contact before reading:
  python3 "$CODE_ROOT/tools/configure_allowlist.py" add +15551234567

Grant Full Disk Access to:
  $CODE_ROOT/bin/cowork-imessage-helper

Then verify:
  python3 "$CODE_ROOT/tools/doctor.py" --bridge "$BRIDGE_ROOT" --code-root "$CODE_ROOT"
EOF
