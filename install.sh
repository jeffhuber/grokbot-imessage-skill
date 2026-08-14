#!/bin/bash
# install.sh — one-time setup for the Grok Bot iMessage helper.
#
# What this does, in order:
#   1. Sanity-checks that we're on macOS with the Xcode Command Line Tools
#      installed (for clang + codesign).
#   2. Creates control/requests, control/responses, and contacts/ if missing.
#   3. Locks down the Python worker and send-gate module.
#   4. Compiles the FDA wrapper and native send-confirmation helper.
#   5. Ad-hoc code-signs the wrapper so macOS can give FDA a stable identity
#      to attach to. Re-signing on content-identical rebuilds keeps the grant.
#   6. Fills in the launchd plist template and installs it under
#      ~/Library/LaunchAgents/com.jeffhuber.grokbot-imessage.plist, then
#      bootstraps it.
#   7. Optionally installs SKILL.md under Grok's user-level skill directory.
#   8. Prints exact next-steps: grant Full Disk Access to the wrapper binary.
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

INSTALL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
BIN_DIR="$INSTALL_ROOT/bin"
CONTROL_DIR="$INSTALL_ROOT/control"
CONTACTS_DIR="$INSTALL_ROOT/contacts"
HELPER_PY="$BIN_DIR/helper.py"
SEND_GATE_PY="$BIN_DIR/send_gate.py"
WRAPPER_SRC="$BIN_DIR/imessage_helper.c"
WRAPPER_BIN="$BIN_DIR/grokbot-imessage-helper"
CONFIRM_SRC="$BIN_DIR/confirm_imessage_send.m"
CONFIRM_BIN="$BIN_DIR/grokbot-imessage-confirm"
PLIST_TEMPLATE="$INSTALL_ROOT/com.jeffhuber.grokbot-imessage.plist.template"
PLIST_DEST="$HOME/Library/LaunchAgents/com.jeffhuber.grokbot-imessage.plist"
LAUNCHCTL_LABEL="com.jeffhuber.grokbot-imessage"
LEGACY_LABEL="com.user.cowork-imessage"
LEGACY_PLIST="$HOME/Library/LaunchAgents/$LEGACY_LABEL.plist"
LEGACY_WRAPPER="$BIN_DIR/cowork-imessage-helper"
LEGACY_MIGRATOR="$INSTALL_ROOT/tools/migrate_legacy_launchagent.py"
INSTALL_GROK_SKILL="${INSTALL_GROK_SKILL:-1}"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
red() { printf "\033[31m%s\033[0m\n" "$*" 1>&2; }

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
        red "Refusing symlinked runtime path: $path"
        exit 1
    fi
    if [[ -e "$path" && "$kind" == "directory" && ! -d "$path" ]]; then
        red "Expected a runtime directory: $path"
        exit 1
    fi
    if [[ -e "$path" && "$kind" == "file" && ! -f "$path" ]]; then
        red "Expected a regular runtime file: $path"
        exit 1
    fi
}

if [[ "$INSTALL_GROK_SKILL" != "0" && "$INSTALL_GROK_SKILL" != "1" ]]; then
    red "INSTALL_GROK_SKILL must be 0 or 1."
    exit 1
fi

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

for cmd in clang codesign launchctl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        red "Required command not found: $cmd"
        exit 1
    fi
done

if ! PYTHON3_PATH="$(find_supported_python)"; then
    red "Python 3.9 or newer with dir_fd support is required."
    exit 1
fi

if [[ ! -f "$WRAPPER_SRC" ]]; then
    red "Missing $WRAPPER_SRC"
    exit 1
fi
if [[ ! -f "$HELPER_PY" ]]; then
    red "Missing $HELPER_PY"
    exit 1
fi
if [[ ! -f "$SEND_GATE_PY" ]]; then
    red "Missing $SEND_GATE_PY"
    exit 1
fi
if [[ ! -f "$CONFIRM_SRC" ]]; then
    red "Missing $CONFIRM_SRC"
    exit 1
fi
if [[ ! -f "$PLIST_TEMPLATE" ]]; then
    red "Missing $PLIST_TEMPLATE"
    exit 1
fi
if [[ ! -f "$LEGACY_MIGRATOR" ]]; then
    red "Missing $LEGACY_MIGRATOR"
    exit 1
fi
if [[ "$INSTALL_GROK_SKILL" == "1" && \
      (! -f "$INSTALL_ROOT/SKILL.md" || ! -x "$INSTALL_ROOT/install-skill.sh") ]]; then
    red "Missing SKILL.md or executable install-skill.sh"
    exit 1
fi
if [[ ! -f "$INSTALL_ROOT/contacts/allowed_chats.txt.template" ]]; then
    red "Missing allowlist template at contacts/allowed_chats.txt.template"
    exit 1
fi

bold "Grok Bot iMessage helper installer"
echo "  install root : $INSTALL_ROOT"
echo "  helper.py    : $HELPER_PY"
echo "  wrapper bin  : $WRAPPER_BIN"
echo "  launchd plist: $PLIST_DEST"
echo

# ---- 2. control / contacts directories -----------------------------------
for path in "$CONTROL_DIR" "$CONTROL_DIR/requests" \
    "$CONTROL_DIR/responses" "$CONTACTS_DIR"; do
    require_safe_runtime_entry "$path" directory
done
for path in "$CONTROL_DIR/log.txt" "$CONTACTS_DIR/blocked_chats.txt" \
    "$CONTACTS_DIR/allowed_chats.txt" "$CONTACTS_DIR/read_policy.txt"; do
    require_safe_runtime_entry "$path" file
done
mkdir -p "$CONTROL_DIR/requests" "$CONTROL_DIR/responses" "$CONTACTS_DIR"
touch "$CONTROL_DIR/log.txt"
chmod 700 "$INSTALL_ROOT" "$CONTROL_DIR" "$CONTROL_DIR/requests" \
    "$CONTROL_DIR/responses" "$CONTACTS_DIR"
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

if [[ ! -f "$CONTACTS_DIR/allowed_chats.txt" ]]; then
    cp "$INSTALL_ROOT/contacts/allowed_chats.txt.template" \
        "$CONTACTS_DIR/allowed_chats.txt"
    chmod 600 "$CONTACTS_DIR/allowed_chats.txt"
    green "  created $CONTACTS_DIR/allowed_chats.txt (empty)"
fi

if [[ ! -f "$CONTACTS_DIR/read_policy.txt" ]]; then
    printf 'blocklist\n' > "$CONTACTS_DIR/read_policy.txt"
    chmod 600 "$CONTACTS_DIR/read_policy.txt"
    green "  created $CONTACTS_DIR/read_policy.txt (blocklist)"
fi

# ---- 3. lock down Python code --------------------------------------------
chmod 500 "$HELPER_PY" "$SEND_GATE_PY"
green "  chmod 500 $HELPER_PY and $SEND_GATE_PY"

# ---- 4. build wrapper binary --------------------------------------------
bold "Building wrapper binary..."

clang -Wall -Wextra -Werror -O2 \
    -DHELPER_SCRIPT="\"$HELPER_PY\"" \
    -DSEND_GATE_SCRIPT="\"$SEND_GATE_PY\"" \
    -DCONFIRM_HELPER="\"$CONFIRM_BIN\"" \
    -DBRIDGE_ROOT="\"$INSTALL_ROOT\"" \
    -DHELPER_DISPLAY_NAME='"grokbot-imessage-helper"' \
    -DHOST_DISPLAY_NAME='"Grok Bot"' \
    -DPYTHON_INTERPRETER="\"$PYTHON3_PATH\"" \
    -o "$WRAPPER_BIN" "$WRAPPER_SRC"
chmod 700 "$WRAPPER_BIN"
green "  built $WRAPPER_BIN"

clang -Wall -Wextra -Werror -fobjc-arc \
    -framework AppKit -framework Foundation \
    -o "$CONFIRM_BIN" "$CONFIRM_SRC"
chmod 700 "$CONFIRM_BIN"
green "  built $CONFIRM_BIN"

# ---- 5. ad-hoc code-sign -------------------------------------------------
# The hardened runtime flag blocks DYLD_INSERT_LIBRARIES et al, so an
# attacker can't hijack our FDA grant via library injection.
codesign --force --sign - --options runtime "$WRAPPER_BIN"
green "  ad-hoc signed $WRAPPER_BIN"
codesign --force --sign - --options runtime "$CONFIRM_BIN"
green "  ad-hoc signed $CONFIRM_BIN"

# Record the CDHash so the user can tell whether a re-sign is needed later.
CDHASH=$(codesign -dvvv "$WRAPPER_BIN" 2>&1 | awk -F'=' '/CDHash=/{print $2; exit}')
echo "  cdhash: ${CDHASH:-unknown}"

# ---- 6. launchd plist ----------------------------------------------------
mkdir -p "$(dirname "$PLIST_DEST")"

# Use Python to generate the plist with proper XML escaping instead of sed.
"$PYTHON3_PATH" - "$INSTALL_ROOT" "$INSTALL_ROOT" "$PLIST_DEST" "$PLIST_TEMPLATE" <<'PYGEN'
import sys, xml.etree.ElementTree as ET

code_root, bridge_root, dest, template = sys.argv[1:]

# Read template and replace placeholder with properly escaped path.
tree = ET.parse(template)
root = tree.getroot()

# Find all <string> elements and replace the path placeholders.
for elem in root.iter("string"):
    if elem.text:
        elem.text = elem.text.replace("{{CODE_ROOT}}", code_root)
        elem.text = elem.text.replace("{{BRIDGE_ROOT}}", bridge_root)

tree.write(dest, encoding="UTF-8", xml_declaration=True)
PYGEN

chmod 644 "$PLIST_DEST"
green "  wrote $PLIST_DEST"

# Claim the legacy identity only when it points to this exact prior Grok
# installation. A Claude installation using the old shared label is left alone.
if [[ -e "$LEGACY_PLIST" || -L "$LEGACY_PLIST" ]]; then
    if "$PYTHON3_PATH" "$LEGACY_MIGRATOR" \
        --plist "$LEGACY_PLIST" \
        --program "$LEGACY_WRAPPER" \
        --watch "$INSTALL_ROOT/control/requests"; then
        if launchctl print "gui/$UID/$LEGACY_LABEL" >/dev/null 2>&1; then
            launchctl bootout "gui/$UID/$LEGACY_LABEL"
        fi
        rm -f "$LEGACY_PLIST"
        green "  migrated this Grok install from legacy label $LEGACY_LABEL"
    else
        yellow "  retained legacy $LEGACY_LABEL because it belongs to another install"
    fi
elif launchctl print "gui/$UID/$LEGACY_LABEL" >/dev/null 2>&1; then
    yellow "  legacy $LEGACY_LABEL is loaded without a verifiable plist; left untouched"
fi

# Bootstrap (or restart) the agent.
if launchctl print "gui/$UID/$LAUNCHCTL_LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$UID/$LAUNCHCTL_LABEL"
    echo "  existing agent unloaded"
fi
launchctl bootstrap "gui/$UID" "$PLIST_DEST"
launchctl enable "gui/$UID/$LAUNCHCTL_LABEL"
green "  launchd agent bootstrapped ($LAUNCHCTL_LABEL)"

if [[ "$INSTALL_GROK_SKILL" == "1" ]]; then
    "$INSTALL_ROOT/install-skill.sh"
else
    yellow "  skipped Grok Build skill copy (INSTALL_GROK_SKILL=0)"
fi

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
echo "  3. Select 'grokbot-imessage-helper' and make sure its toggle is ON."
echo "  4. (If prompted to quit and reopen anything, just click 'Later'.)"
echo
echo "Verify by asking Grok Bot: \"review my imessages over the last 2 days\""
echo
yellow "Note on sending (v0.3.0+):"
echo "  The first time you ask Grok Bot to send an iMessage, macOS will"
echo "  prompt 'grokbot-imessage-helper wants to control Messages.' Click OK."
echo "  After that, the grant lives under:"
echo "    System Settings -> Privacy & Security -> Automation"
echo "  (This is a separate permission from Full Disk Access.)"
echo
echo "Logs: $CONTROL_DIR/log.txt"
if [[ "$INSTALL_GROK_SKILL" == "1" ]]; then
    echo "Doctor: \"$PYTHON3_PATH\" $INSTALL_ROOT/tools/doctor.py --bridge $INSTALL_ROOT"
else
    echo "Doctor: \"$PYTHON3_PATH\" $INSTALL_ROOT/tools/doctor.py --bridge $INSTALL_ROOT --skip-grok"
fi
echo "Uninstall: ./uninstall.sh"
