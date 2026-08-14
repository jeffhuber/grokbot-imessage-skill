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

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PRODUCT_ROOT="/Library/Application Support/GrokBotIMessage"
USER_ROOT="$PRODUCT_ROOT/users/$UID"
CODE_ROOT="$USER_ROOT/libexec"
CONFIG_ROOT="$USER_ROOT/config"
BRIDGE_ROOT="${GROKBOT_IMESSAGE_BRIDGE:-$HOME/Library/Application Support/GrokBotIMessage}"
PLIST_TEMPLATE="$SOURCE_ROOT/com.jeffhuber.grokbot-imessage.plist.template"
PLIST_DEST="$HOME/Library/LaunchAgents/com.jeffhuber.grokbot-imessage.plist"
LABEL="com.jeffhuber.grokbot-imessage"
LEGACY_LABEL="com.user.cowork-imessage"
LEGACY_PLIST="$HOME/Library/LaunchAgents/$LEGACY_LABEL.plist"
LEGACY_WRAPPER="$CODE_ROOT/bin/cowork-imessage-helper"
LEGACY_MIGRATOR="$SOURCE_ROOT/tools/migrate_legacy_launchagent.py"
ALLOWLIST="$CONFIG_ROOT/allowed_chats.txt"
CURRENT_USER="$(id -un)"
BUILD_DIR="$(mktemp -d -t grokbot-imessage-build.XXXXXX)"
trap 'rm -rf "$BUILD_DIR"' EXIT

find_supported_python() {
    local candidate
    local resolved
    for candidate in "${IMESSAGE_PYTHON:-}" /usr/bin/python3 \
        python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
        [[ -n "$candidate" ]] || continue
        if [[ "$candidate" == */* ]]; then
            resolved="$candidate"
        else
            resolved="$(command -v "$candidate" 2>/dev/null || true)"
        fi
        if [[ -x "$resolved" ]] &&
            "$resolved" -c 'import os, sys; raise SystemExit(sys.version_info < (3, 9) or os.open not in os.supports_dir_fd)' 2>/dev/null; then
            printf '%s\n' "$resolved"
            return 0
        fi
    done
    return 1
}

require_safe_runtime_entry() {
    local path="$1"
    local kind="$2"
    if [[ -L "$path" ]]; then
        echo "Error: refusing symlinked runtime path: $path" >&2
        exit 1
    fi
    if [[ -e "$path" && "$kind" == "directory" && ! -d "$path" ]]; then
        echo "Error: expected a runtime directory: $path" >&2
        exit 1
    fi
    if [[ -e "$path" && "$kind" == "file" && ! -f "$path" ]]; then
        echo "Error: expected a regular runtime file: $path" >&2
        exit 1
    fi
}

for cmd in clang codesign launchctl sudo; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "Error: required command not found: $cmd" >&2
        exit 1
    fi
done
if ! xcode-select -p >/dev/null 2>&1; then
    echo "Error: install Xcode Command Line Tools with xcode-select --install" >&2
    exit 1
fi
if ! PYTHON3_PATH="$(find_supported_python)"; then
    echo "Error: Python 3.9 or newer with dir_fd support is required." >&2
    exit 1
fi

for path in \
    "$SOURCE_ROOT/bin/helper.py" \
    "$SOURCE_ROOT/bin/send_gate.py" \
    "$SOURCE_ROOT/bin/imessage_helper.c" \
    "$SOURCE_ROOT/bin/confirm_imessage_send.m" \
    "$SOURCE_ROOT/tools/doctor.py" \
    "$SOURCE_ROOT/tools/configure_allowlist.py" \
    "$LEGACY_MIGRATOR" \
    "$SOURCE_ROOT/contacts/blocked_chats.txt.template" \
    "$SOURCE_ROOT/contacts/allowed_chats.txt.template" \
    "$SOURCE_ROOT/install-skill.sh" \
    "$PLIST_TEMPLATE"; do
    if [[ ! -f "$path" ]]; then
        echo "Error: missing source file: $path" >&2
        exit 1
    fi
done

for path in "$BRIDGE_ROOT/control" "$BRIDGE_ROOT/control/requests" \
    "$BRIDGE_ROOT/control/responses" "$BRIDGE_ROOT/contacts"; do
    require_safe_runtime_entry "$path" directory
done
for path in "$BRIDGE_ROOT/control/log.txt" \
    "$BRIDGE_ROOT/contacts/blocked_chats.txt" \
    "$BRIDGE_ROOT/contacts/read_policy.txt"; do
    require_safe_runtime_entry "$path" file
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
if ! "$PYTHON3_PATH" - "$ALLOWLIST" <<'PYCHECK'; then
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

clang -Wall -Wextra -Werror -fobjc-arc \
    -framework AppKit -framework Foundation \
    -o "$BUILD_DIR/grokbot-imessage-confirm" "$SOURCE_ROOT/bin/confirm_imessage_send.m"

clang -Wall -Wextra -Werror -O2 \
    -DHELPER_SCRIPT="\"$CODE_ROOT/bin/helper.py\"" \
    -DSEND_GATE_SCRIPT="\"$CODE_ROOT/bin/send_gate.py\"" \
    -DCONFIRM_HELPER="\"$CODE_ROOT/bin/grokbot-imessage-confirm\"" \
    -DBRIDGE_ROOT="\"$BRIDGE_ROOT\"" \
    -DPYTHON_INTERPRETER="\"$PYTHON3_PATH\"" \
    -DEXPECTED_CODE_UID=0 \
    -DREAD_POLICY_MODE='"allowlist"' \
    -DREAD_ALLOWLIST_PATH="\"$ALLOWLIST\"" \
    -DREQUIRE_ROOT_POLICY=1 \
    -DHELPER_DISPLAY_NAME='"grokbot-imessage-helper"' \
    -DHOST_DISPLAY_NAME='"Grok Bot"' \
    -o "$BUILD_DIR/grokbot-imessage-helper" \
    "$SOURCE_ROOT/bin/imessage_helper.c"

CODESIGN_IDENTITY="${CODESIGN_IDENTITY:--}"
SIGN_ARGS=(--force --sign "$CODESIGN_IDENTITY" --options runtime)
if [[ "$CODESIGN_IDENTITY" != "-" ]]; then
    SIGN_ARGS+=(--timestamp)
fi
codesign "${SIGN_ARGS[@]}" "$BUILD_DIR/grokbot-imessage-helper"
codesign "${SIGN_ARGS[@]}" "$BUILD_DIR/grokbot-imessage-confirm"

sudo /usr/bin/install -o root -g wheel -m 444 \
    "$SOURCE_ROOT/bin/helper.py" "$CODE_ROOT/bin/helper.py"
sudo /usr/bin/install -o root -g wheel -m 444 \
    "$SOURCE_ROOT/bin/send_gate.py" "$CODE_ROOT/bin/send_gate.py"
sudo /usr/bin/install -o root -g wheel -m 444 \
    "$SOURCE_ROOT/bin/imessage_helper.c" "$CODE_ROOT/bin/imessage_helper.c"
sudo /usr/bin/install -o root -g wheel -m 444 \
    "$SOURCE_ROOT/bin/confirm_imessage_send.m" "$CODE_ROOT/bin/confirm_imessage_send.m"
sudo /usr/bin/install -o root -g wheel -m 555 \
    "$BUILD_DIR/grokbot-imessage-helper" "$CODE_ROOT/bin/grokbot-imessage-helper"
sudo /usr/bin/install -o root -g wheel -m 555 \
    "$BUILD_DIR/grokbot-imessage-confirm" "$CODE_ROOT/bin/grokbot-imessage-confirm"
sudo /usr/bin/install -o root -g wheel -m 555 \
    "$SOURCE_ROOT/tools/doctor.py" "$CODE_ROOT/tools/doctor.py"
sudo /usr/bin/install -o root -g wheel -m 555 \
    "$SOURCE_ROOT/tools/configure_allowlist.py" "$CODE_ROOT/tools/configure_allowlist.py"

mkdir -p "$(dirname "$PLIST_DEST")"
"$PYTHON3_PATH" - "$CODE_ROOT" "$BRIDGE_ROOT" "$PLIST_DEST" "$PLIST_TEMPLATE" <<'PYGEN'
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

if [[ -e "$LEGACY_PLIST" || -L "$LEGACY_PLIST" ]]; then
    if "$PYTHON3_PATH" "$LEGACY_MIGRATOR" \
        --plist "$LEGACY_PLIST" \
        --program "$LEGACY_WRAPPER" \
        --watch "$BRIDGE_ROOT/control/requests"; then
        if launchctl print "gui/$UID/$LEGACY_LABEL" >/dev/null 2>&1; then
            launchctl bootout "gui/$UID/$LEGACY_LABEL"
        fi
        rm -f "$LEGACY_PLIST"
        echo "  migrated this Grok install from legacy label $LEGACY_LABEL"
    else
        echo "  retained legacy $LEGACY_LABEL because it belongs to another install"
    fi
elif launchctl print "gui/$UID/$LEGACY_LABEL" >/dev/null 2>&1; then
    echo "  legacy $LEGACY_LABEL is loaded without a verifiable plist; left untouched"
fi

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
  "$PYTHON3_PATH" "$CODE_ROOT/tools/configure_allowlist.py" add +15551234567

Grant Full Disk Access to:
  $CODE_ROOT/bin/grokbot-imessage-helper

Then verify:
  "$PYTHON3_PATH" "$CODE_ROOT/tools/doctor.py" --bridge "$BRIDGE_ROOT" --code-root "$CODE_ROOT"
EOF
