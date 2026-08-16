#!/bin/bash
# Test script for CORE-2: dual-mode wrapper build and product allowlist
# Acceptance test: Baked-mode behavior byte-identical; product mode rejects
# `--product /tmp/x`, `Claude`, extra args with exit 8 and no exec

set -euo pipefail

echo "=== CORE-2 Test Suite ==="
echo

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
echo

# Test 2: Product mode compilation
echo "Test 2: Product mode compilation"
clang -Wall -Wextra -Werror -O2 \
  -DIMESSAGE_PRODUCT_BUILD=1 \
  -DHELPER_DISPLAY_NAME='"test-helper"' \
  -o /tmp/test-product-helper bin/imessage_helper.c
echo "✓ Product mode compiles successfully"
echo

# Test 3: Mutual exclusivity (should fail to compile)
echo "Test 3: Mutual exclusivity check"
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

# Test 4: Product mode rejects path-like argument
echo "Test 4: Reject path-like product ID"
set +e
/tmp/test-product-helper --product /tmp/x 2>/dev/null
exit_code=$?
set -e
if [ $exit_code -ne 8 ]; then
  echo "✗ FAIL: Exit code should be 8, got $exit_code"
  exit 1
fi
echo "✓ Path-like product ID rejected with exit 8"
echo

# Test 5: Product mode rejects case-wrong argument
echo "Test 5: Reject case-wrong product ID"
set +e
/tmp/test-product-helper --product Claude 2>/dev/null
exit_code=$?
set -e
if [ $exit_code -ne 8 ]; then
  echo "✗ FAIL: Exit code should be 8, got $exit_code"
  exit 1
fi
echo "✓ Case-wrong product ID rejected with exit 8"
echo

# Test 6: Product mode rejects extra arguments
echo "Test 6: Reject extra arguments"
set +e
/tmp/test-product-helper --product openai extra-arg 2>/dev/null
exit_code=$?
set -e
if [ $exit_code -ne 8 ]; then
  echo "✗ FAIL: Exit code should be 8, got $exit_code"
  exit 1
fi
echo "✓ Extra arguments rejected with exit 8"
echo

# Test 7: Product mode requires --product
echo "Test 7: Require --product argument"
set +e
/tmp/test-product-helper 2>/dev/null
exit_code=$?
set -e
if [ $exit_code -ne 8 ]; then
  echo "✗ FAIL: Exit code should be 8, got $exit_code"
  exit 1
fi
echo "✓ Missing --product rejected with exit 8"
echo

# Test 8: Product mode rejects duplicate --product arguments
echo "Test 8: Reject duplicate --product arguments"
set +e
/tmp/test-product-helper --product claude --product grok --validate-only 2>/dev/null
exit_code=$?
set -e
if [ $exit_code -ne 8 ]; then
  echo "✗ FAIL: Duplicate --product should exit 8, got $exit_code"
  exit 1
fi
echo "✓ Duplicate --product rejected with exit 8"
echo

# Test 9: Product mode rejects duplicate --validate-only arguments
echo "Test 9: Reject duplicate --validate-only arguments"
set +e
/tmp/test-product-helper --product openai --validate-only --validate-only 2>/dev/null
exit_code=$?
set -e
if [ $exit_code -ne 8 ]; then
  echo "✗ FAIL: Duplicate --validate-only should exit 8, got $exit_code"
  exit 1
fi
echo "✓ Duplicate --validate-only rejected with exit 8"
echo

# Test 10: Validate-only for all product IDs
echo "Test 10: Validate-only for all product IDs"
for id in claude grok openai manager; do
  output=$(/tmp/test-product-helper --product "$id" --validate-only)
  if [ $? -ne 0 ]; then
    echo "✗ FAIL: --product $id --validate-only should succeed"
    exit 1
  fi
  if ! echo "$output" | grep -q "\"product\":\"$id\""; then
    echo "✗ FAIL: JSON output should contain product ID $id"
    exit 1
  fi
  if ! echo "$output" | grep -q "\"validate_only\":true"; then
    echo "✗ FAIL: JSON output should contain validate_only:true"
    exit 1
  fi
  echo "  ✓ $id: $output"
done
echo "✓ All product IDs validate correctly"
echo

# Test 11: Product mode without --validate-only returns exit 1
echo "Test 11: Product mode exec stub returns exit 1"
set +e
/tmp/test-product-helper --product openai 2>/dev/null
exit_code=$?
set -e
if [ $exit_code -ne 1 ]; then
  echo "✗ FAIL: Exit code should be 1 (not implemented), got $exit_code"
  exit 1
fi
echo "✓ Product mode exec stub correctly returns exit 1"
echo

echo "=== All CORE-2 tests passed ==="
