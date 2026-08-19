#!/bin/bash
# Test script for CORE-2, CORE-3, and CORE-4: product wrapper validation
# CORE-2 acceptance: Baked-mode behavior byte-identical; product mode runs the
# bundled interpreter with -I -B and rejects `--product /tmp/x`, `Claude`, and
# extra args with exit 8 and no exec.
# CORE-3 acceptance: --validate-only prints distinct roots for four ids

set -euo pipefail

# Detect OS - full tests on macOS, syntax-only on Linux
IS_MACOS=false
if [[ "$OSTYPE" == "darwin"* ]]; then
  IS_MACOS=true
fi

TEMP_DIR=""
TEST_HELPER=""
BAKED_HELPER=""

cleanup() {
  if [ -n "$TEMP_DIR" ] && [ -d "$TEMP_DIR" ]; then
    rm -rf "$TEMP_DIR"
  fi
  if [ -n "$TEST_HELPER" ] && [ -f "$TEST_HELPER" ]; then
    rm -f "$TEST_HELPER"
  fi
  if [ -n "$BAKED_HELPER" ] && [ -f "$BAKED_HELPER" ]; then
    rm -f "$BAKED_HELPER"
  fi
}

trap cleanup EXIT

echo "=== CORE-2 + CORE-3 + CORE-4 Test Suite ==="
if [ "$IS_MACOS" = false ]; then
  echo "Running on Linux - syntax checks only"
fi
echo

PRODUCT_DEFINES=(
  -DIMESSAGE_PRODUCT_BUILD=1
  -DAPP_SUPPORT_DIRNAME='"TestBridgePro"'
  -DPYTHON_RELPATH='"Versions/A/Python"'
  -DIMESSAGE_BUNDLE_ID='"com.test.bridgepro"'
  -DIMESSAGE_CONFIRM_BUNDLE_ID='"com.test.bridgepro.confirm"'
  -DIMESSAGE_PYTHON_BUNDLE_ID='"org.python.python"'
  -DIMESSAGE_TEAM_ID='"TESTTEAMID"'
  -DHELPER_DISPLAY_NAME='"test-helper"'
)

PRODUCT_TEST_REQUIREMENT_DEFINES=(
  '-DIMESSAGE_BUNDLE_REQUIREMENT="identifier \"com.test.bridgepro\""'
  '-DIMESSAGE_CONFIRM_REQUIREMENT="identifier \"com.test.bridgepro.confirm\""'
  '-DIMESSAGE_PYTHON_REQUIREMENT="identifier \"org.python.python\""'
)

PRODUCT_LINK_FLAGS=()
if [ "$IS_MACOS" = true ]; then
  PRODUCT_LINK_FLAGS=(-framework Security -framework CoreFoundation)
fi

# Test 1: Baked mode compilation (existing behavior)
echo "Test 1: Baked mode compilation"
clang -Wall -Wextra -Werror -O2 \
  -DHELPER_SCRIPT='"/tmp/helper.py"' \
  -DSEND_GATE_SCRIPT='"/tmp/send_gate.py"' \
  -DCONFIRM_HELPER='"/tmp/confirm"' \
  -DBRIDGE_ROOT='"/tmp/bridge"' \
  -DHELPER_DISPLAY_NAME='"test-helper"' \
  -fsyntax-only bin/imessage_helper.c
echo "✓ Baked mode compiles successfully"
if [ "$IS_MACOS" = true ]; then
  BAKED_HELPER=$(mktemp)
  clang -Wall -Wextra -Werror -O2 \
    -DHELPER_SCRIPT='"/tmp/helper.py"' \
    -DSEND_GATE_SCRIPT='"/tmp/send_gate.py"' \
    -DCONFIRM_HELPER='"/tmp/confirm"' \
    -DBRIDGE_ROOT='"/tmp/bridge"' \
    -DHELPER_DISPLAY_NAME='"test-helper"' \
    -o "$BAKED_HELPER" bin/imessage_helper.c
  if otool -L "$BAKED_HELPER" | grep -q "Security.framework"; then
    echo "✗ FAIL: Baked/DIY build must not link Security.framework"
    exit 1
  fi
  echo "✓ Baked/DIY build has no Security.framework dependency"
fi
echo

# Test 2: Product mode compilation (syntax check only, no macros)
echo "Test 2: Product mode compilation requires macros"
if clang -Wall -Wextra -Werror -O2 \
  -DIMESSAGE_PRODUCT_BUILD=1 \
  -DHELPER_DISPLAY_NAME='"test-helper"' \
  -fsyntax-only bin/imessage_helper.c 2>/dev/null; then
  echo "✗ FAIL: Product build should require APP_SUPPORT_DIRNAME and PYTHON_RELPATH"
  exit 1
fi
echo "✓ Product mode correctly requires macros"
echo

# Test 3: Product mode compiles with required macros
echo "Test 3: Product mode compilation with macros"
if [ "$IS_MACOS" = true ]; then
  TEST_HELPER=$(mktemp)
  clang -Wall -Wextra -Werror -O2 \
    "${PRODUCT_DEFINES[@]}" \
    -o "$TEST_HELPER" bin/imessage_helper.c \
    "${PRODUCT_LINK_FLAGS[@]}"
  echo "✓ Product mode compiles with required macros"
else
  clang -Wall -Wextra -Werror -O2 \
    "${PRODUCT_DEFINES[@]}" \
    -fsyntax-only bin/imessage_helper.c
  echo "✓ Product mode compiles with required macros (syntax check)"
fi
echo

# Test 4: Mutual exclusivity (should fail to compile)
echo "Test 4: Mutual exclusivity check"
if clang -Wall -Wextra -Werror -O2 \
  -DIMESSAGE_PRODUCT_BUILD=1 \
  -DHELPER_SCRIPT='"/tmp/helper.py"' \
  -DSEND_GATE_SCRIPT='"/tmp/send_gate.py"' \
  -DCONFIRM_HELPER='"/tmp/confirm"' \
  -DBRIDGE_ROOT='"/tmp/bridge"' \
  -fsyntax-only bin/imessage_helper.c 2>/dev/null; then
  echo "✗ FAIL: Product build should reject baked path macros"
  exit 1
fi
echo "✓ Product build correctly rejects baked path macros"
echo

# Test 5: Product mode rejects path-like argument
if [ "$IS_MACOS" = true ]; then
  echo "Test 5: Reject path-like product ID"
  set +e
  "$TEST_HELPER" --product /tmp/x 2>/dev/null
  exit_code=$?
  set -e
  if [ $exit_code -ne 8 ]; then
    echo "✗ FAIL: Exit code should be 8, got $exit_code"
    exit 1
  fi
  echo "✓ Path-like product ID rejected with exit 8"
  echo

  # Test 6: Product mode rejects case-wrong argument
  echo "Test 6: Reject case-wrong product ID"
  set +e
  "$TEST_HELPER" --product Claude 2>/dev/null
  exit_code=$?
  set -e
  if [ $exit_code -ne 8 ]; then
    echo "✗ FAIL: Exit code should be 8, got $exit_code"
    exit 1
  fi
  echo "✓ Case-wrong product ID rejected with exit 8"
  echo

  # Test 7: Product mode rejects extra arguments
  echo "Test 7: Reject extra arguments"
  set +e
  "$TEST_HELPER" --product openai extra-arg 2>/dev/null
  exit_code=$?
  set -e
  if [ $exit_code -ne 8 ]; then
    echo "✗ FAIL: Exit code should be 8, got $exit_code"
    exit 1
  fi
  echo "✓ Extra arguments rejected with exit 8"
  echo

  # Test 8: Product mode requires --product
  echo "Test 8: Require --product argument"
  set +e
  "$TEST_HELPER" 2>/dev/null
  exit_code=$?
  set -e
  if [ $exit_code -ne 8 ]; then
    echo "✗ FAIL: Exit code should be 8, got $exit_code"
    exit 1
  fi
  echo "✓ Missing --product rejected with exit 8"
  echo

  # Test 9: Product mode rejects duplicate --product arguments
  echo "Test 9: Reject duplicate --product arguments"
  set +e
  "$TEST_HELPER" --product claude --product grok --validate-only 2>/dev/null
  exit_code=$?
  set -e
  if [ $exit_code -ne 8 ]; then
    echo "✗ FAIL: Duplicate --product should exit 8, got $exit_code"
    exit 1
  fi
  echo "✓ Duplicate --product rejected with exit 8"
  echo

  # Test 10: Product mode rejects duplicate --validate-only arguments
  echo "Test 10: Reject duplicate --validate-only arguments"
  set +e
  "$TEST_HELPER" --product openai --validate-only --validate-only 2>/dev/null
  exit_code=$?
  set -e
  if [ $exit_code -ne 8 ]; then
    echo "✗ FAIL: Duplicate --validate-only should exit 8, got $exit_code"
    exit 1
  fi
  echo "✓ Duplicate --validate-only rejected with exit 8"
  echo

  # Test 11: Validate-only requires bundle structure
  echo "Test 11: Validate-only requires bundle (exits 9 without)"
  set +e
  output=$("$TEST_HELPER" --product claude --validate-only 2>&1)
  exit_code=$?
  set -e
  if [ $exit_code -ne 9 ]; then
    echo "✗ FAIL: Exit code should be 9 (bundle not found), got $exit_code"
    echo "  Output: $output"
    exit 1
  fi
  echo "✓ Validate-only correctly requires bundle structure"
  echo

  # Test 12: Product mode without --validate-only exits 9 (not in bundle)
  echo "Test 12: Product mode exec exits 9 when not in bundle"
  set +e
  "$TEST_HELPER" --product openai 2>/dev/null
  exit_code=$?
  set -e
  if [ $exit_code -ne 9 ]; then
    echo "✗ FAIL: Exit code should be 9 (bundle not found), got $exit_code"
    exit 1
  fi
  echo "✓ Product mode exec correctly returns exit 9"
  echo
fi

echo "=== CORE-3 Tests ==="
echo

if [ "$IS_MACOS" = false ]; then
  echo "Skipping bundle tests on Linux (macOS-only)"
  echo "=== All CORE-2 + CORE-3 + CORE-4 tests passed (syntax checks) ==="
  exit 0
fi

# Test 13: Create fake bundle structure and test distinct roots
echo "Test 13: Distinct roots for four product IDs"
TEMP_DIR=$(mktemp -d)

# Create fake bundle structure
BUNDLE_PATH="$TEMP_DIR/TestApp.app"
mkdir -p "$BUNDLE_PATH/Contents/MacOS"
mkdir -p "$BUNDLE_PATH/Contents/Helpers"
mkdir -p "$BUNDLE_PATH/Contents/Resources/core/bin"
mkdir -p "$BUNDLE_PATH/Contents/Frameworks/Python.framework/Versions/A/Resources"

# Create Info.plist
echo '<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>TestApp</string>
    <key>CFBundleIdentifier</key>
    <string>com.test.bridgepro</string>
</dict>
</plist>' > "$BUNDLE_PATH/Contents/Info.plist"

# Create dummy files
cat > "$TEMP_DIR/test_app.c" <<'C'
int main(void) { return 0; }
C
clang -Wall -Wextra -Werror -O2 -o "$BUNDLE_PATH/Contents/MacOS/TestApp" "$TEMP_DIR/test_app.c"

touch "$BUNDLE_PATH/Contents/Resources/core/bin/helper.py"
touch "$BUNDLE_PATH/Contents/Resources/core/bin/send_gate.py"
printf "test icon" > "$BUNDLE_PATH/Contents/Resources/AppIcon.icns"
chmod 644 "$BUNDLE_PATH/Contents/Resources/AppIcon.icns"
cat > "$TEMP_DIR/python.c" <<'C'
int main(void) { return 0; }
C
clang -Wall -Wextra -Werror -O2 -o "$BUNDLE_PATH/Contents/Frameworks/Python.framework/Versions/A/Python" "$TEMP_DIR/python.c"
cat > "$BUNDLE_PATH/Contents/Frameworks/Python.framework/Versions/A/Resources/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>Python</string>
    <key>CFBundleIdentifier</key>
    <string>org.python.python</string>
    <key>CFBundlePackageType</key>
    <string>FMWK</string>
</dict>
</plist>
PLIST
chmod 700 "$BUNDLE_PATH/Contents/Frameworks/Python.framework/Versions/A/Python"
(cd "$BUNDLE_PATH/Contents/Frameworks/Python.framework" && \
  ln -s A Versions/Current && \
  ln -s Versions/Current/Python Python && \
  ln -s Versions/Current/Resources Resources)
codesign --force --sign - --identifier org.python.python \
  "$BUNDLE_PATH/Contents/Frameworks/Python.framework" >/dev/null

cat > "$TEMP_DIR/confirm.c" <<'C'
int main(void) { return 0; }
C
clang -Wall -Wextra -Werror -O2 -o "$BUNDLE_PATH/Contents/Helpers/imessage-confirm" "$TEMP_DIR/confirm.c"
codesign --force --sign - --identifier com.test.bridgepro.confirm \
  "$BUNDLE_PATH/Contents/Helpers/imessage-confirm" >/dev/null

# Compile product-mode helper into the bundle
clang -Wall -Wextra -Werror -O2 \
  "${PRODUCT_DEFINES[@]}" \
  "${PRODUCT_TEST_REQUIREMENT_DEFINES[@]}" \
  -o "$BUNDLE_PATH/Contents/Helpers/test-helper" \
  bin/imessage_helper.c \
  "${PRODUCT_LINK_FLAGS[@]}"

codesign --force --sign - --identifier com.test.bridgepro "$BUNDLE_PATH" >/dev/null

echo "  ✓ Compiled product-mode helper in bundle"

# Test distinct roots for each product ID
for id in claude grok openai manager; do
  if ! output=$("$BUNDLE_PATH/Contents/Helpers/test-helper" --product "$id" --validate-only); then
    echo "✗ FAIL: --product $id --validate-only should succeed"
    exit 1
  fi

  if ! echo "$output" | grep -q "\"product\":\"$id\""; then
    echo "✗ FAIL: JSON output should contain product ID $id"
    exit 1
  fi

  if ! echo "$output" | grep -q "\"bridge_root\":"; then
    echo "✗ FAIL: JSON output should contain bridge_root"
    exit 1
  fi

  if ! echo "$output" | grep -q "/TestBridgePro/bridges/$id"; then
    echo "✗ FAIL: Bridge root should contain /TestBridgePro/bridges/$id"
    exit 1
  fi

  # Check policy_dir for host roles only
  if [ "$id" != "manager" ]; then
    if ! echo "$output" | grep -q "\"policy_dir\":"; then
      echo "✗ FAIL: Host role should have policy_dir"
      exit 1
    fi
    if ! echo "$output" | grep -q "/TestBridgePro/policies/$id"; then
      echo "✗ FAIL: Policy dir should contain /TestBridgePro/policies/$id"
      exit 1
    fi
  else
    if echo "$output" | grep -q "\"policy_dir\":"; then
      echo "✗ FAIL: Manager role should not have policy_dir"
      exit 1
    fi
  fi

  echo "  ✓ $id: distinct root verified"
done
echo "✓ All product IDs have distinct roots"
echo

# Test 14: Verify bundle path resolution
echo "Test 14: Bundle path components in output"
output=$("$BUNDLE_PATH/Contents/Helpers/test-helper" --product claude --validate-only)
if ! echo "$output" | grep -q "\"helper_py\":\".*TestApp.app/Contents/Resources/core/bin/helper.py\""; then
  echo "✗ FAIL: helper_py path not resolved correctly"
  exit 1
fi
if ! echo "$output" | grep -q "\"python_interp\":\".*Python.framework/Versions/A/Python\""; then
  echo "✗ FAIL: python_interp path not resolved correctly"
  exit 1
fi
if ! echo "$output" | grep -q "\"host_icon\":\".*TestApp.app/Contents/Resources/AppIcon.icns\""; then
  echo "✗ FAIL: host_icon path not resolved correctly"
  exit 1
fi
echo "✓ Bundle-relative paths resolved correctly"
echo

# Test 15: Product validate-only tolerates bundles without an icon
echo "Test 15: Product validate-only tolerates missing host icon"
mv "$BUNDLE_PATH/Contents/Resources/AppIcon.icns" "$BUNDLE_PATH/Contents/Resources/AppIcon.icns.missing"
codesign --force --sign - --identifier com.test.bridgepro "$BUNDLE_PATH" >/dev/null
if ! "$BUNDLE_PATH/Contents/Helpers/test-helper" --product claude --validate-only >/dev/null; then
  echo "✗ FAIL: Missing optional host icon should not block validate-only"
  exit 1
fi
mv "$BUNDLE_PATH/Contents/Resources/AppIcon.icns.missing" "$BUNDLE_PATH/Contents/Resources/AppIcon.icns"
codesign --force --sign - --identifier com.test.bridgepro "$BUNDLE_PATH" >/dev/null
echo "✓ Missing optional host icon tolerated"
echo

# Test 16: Validation rejects non-executable interpreter
echo "Test 16: Product validate-only rejects non-executable Python"
chmod 600 "$BUNDLE_PATH/Contents/Frameworks/Python.framework/Versions/A/Python"
set +e
"$BUNDLE_PATH/Contents/Helpers/test-helper" --product claude --validate-only >/dev/null 2>&1
exit_code=$?
set -e
if [ $exit_code -ne 6 ]; then
  echo "✗ FAIL: Non-executable Python should exit 6, got $exit_code"
  exit 1
fi
chmod 700 "$BUNDLE_PATH/Contents/Frameworks/Python.framework/Versions/A/Python"
echo "✓ Non-executable Python rejected with exit 6"
echo

# Test 17: Validation rejects executables the current user cannot run
echo "Test 17: Product validate-only rejects inaccessible execute bits"
chmod 001 "$BUNDLE_PATH/Contents/Frameworks/Python.framework/Versions/A/Python"
set +e
"$BUNDLE_PATH/Contents/Helpers/test-helper" --product claude --validate-only >/dev/null 2>&1
exit_code=$?
set -e
if [ $exit_code -ne 6 ]; then
  echo "✗ FAIL: Inaccessible execute bit should exit 6, got $exit_code"
  exit 1
fi
chmod 700 "$BUNDLE_PATH/Contents/Frameworks/Python.framework/Versions/A/Python"
echo "✓ Inaccessible execute bit rejected with exit 6"
echo

# Test 18: Product validate-only rejects writable host icons
echo "Test 18: Product validate-only rejects writable host icon"
chmod 666 "$BUNDLE_PATH/Contents/Resources/AppIcon.icns"
set +e
"$BUNDLE_PATH/Contents/Helpers/test-helper" --product claude --validate-only >/dev/null 2>&1
exit_code=$?
set -e
if [ $exit_code -ne 5 ]; then
  echo "✗ FAIL: Writable host icon should exit 5, got $exit_code"
  exit 1
fi
chmod 644 "$BUNDLE_PATH/Contents/Resources/AppIcon.icns"
echo "✓ Writable host icon rejected with exit 5"
echo

# Test 19: Product validate-only rejects bundle seal tampering
echo "Test 19: Product validate-only rejects bundle seal tampering"
echo "# tampered" >> "$BUNDLE_PATH/Contents/Resources/core/bin/helper.py"
set +e
"$BUNDLE_PATH/Contents/Helpers/test-helper" --product claude --validate-only >/dev/null 2>&1
exit_code=$?
set -e
if [ $exit_code -ne 10 ]; then
  echo "✗ FAIL: Tampered sealed bundle should exit 10, got $exit_code"
  exit 1
fi
echo "✓ Tampered sealed bundle rejected with exit 10"
echo

# Re-seal the bundle, then tamper with the separately validated Python leaf.
printf "" > "$BUNDLE_PATH/Contents/Resources/core/bin/helper.py"
codesign --force --sign - --identifier com.test.bridgepro "$BUNDLE_PATH" >/dev/null

echo "Test 20: Product validate-only rejects Python interpreter tampering"
printf "tamper" >> "$BUNDLE_PATH/Contents/Frameworks/Python.framework/Versions/A/Python"
set +e
"$BUNDLE_PATH/Contents/Helpers/test-helper" --product openai --validate-only >/dev/null 2>&1
exit_code=$?
set -e
if [ $exit_code -ne 10 ]; then
  echo "✗ FAIL: Tampered Python interpreter should exit 10, got $exit_code"
  exit 1
fi
echo "✓ Tampered Python interpreter rejected with exit 10"
echo

# Restore Python and the bundle, then tamper with imessage-confirm.
clang -Wall -Wextra -Werror -O2 -o "$BUNDLE_PATH/Contents/Frameworks/Python.framework/Versions/A/Python" "$TEMP_DIR/python.c"
chmod 700 "$BUNDLE_PATH/Contents/Frameworks/Python.framework/Versions/A/Python"
codesign --force --sign - --identifier org.python.python \
  "$BUNDLE_PATH/Contents/Frameworks/Python.framework" >/dev/null
codesign --force --sign - --identifier com.test.bridgepro "$BUNDLE_PATH" >/dev/null

echo "Test 21: Product validate-only rejects confirm helper tampering"
printf "tamper" >> "$BUNDLE_PATH/Contents/Helpers/imessage-confirm"
set +e
"$BUNDLE_PATH/Contents/Helpers/test-helper" --product openai --validate-only >/dev/null 2>&1
exit_code=$?
set -e
if [ $exit_code -ne 10 ]; then
  echo "✗ FAIL: Tampered confirm helper should exit 10, got $exit_code"
  exit 1
fi
echo "✓ Tampered confirm helper rejected with exit 10"
echo

echo "=== All CORE-2 + CORE-3 + CORE-4 tests passed ==="
