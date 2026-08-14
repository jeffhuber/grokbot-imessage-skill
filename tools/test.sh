#!/bin/bash
# Run the repository's Python checks with one explicitly validated interpreter.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SELECTOR="$REPO_ROOT/tools/select_python.sh"

if [[ ! -f "$SELECTOR" || -L "$SELECTOR" ]]; then
    echo "Error: missing regular Python selector: $SELECTOR" >&2
    exit 1
fi
# shellcheck source=tools/select_python.sh
source "$SELECTOR"

if [[ "$#" -gt 1 ]]; then
    echo "Usage: $0 [vX.Y.Z]" >&2
    exit 2
fi

if [[ "${IMESSAGE_TEST_PYTHON+x}" == "x" ]]; then
    if [[ "$IMESSAGE_TEST_PYTHON" != /* ]] ||
        ! _imessage_python_is_supported "$IMESSAGE_TEST_PYTHON"; then
        echo "Error: IMESSAGE_TEST_PYTHON must be an absolute path to a supported Python 3.9+ interpreter." >&2
        exit 1
    fi
    TEST_PYTHON="$IMESSAGE_TEST_PYTHON"
elif ! TEST_PYTHON="$(find_supported_python 0)"; then
    echo "Error: no supported Python 3.9+ interpreter with dir_fd support was found." >&2
    exit 1
fi

cd "$REPO_ROOT"
printf 'Test interpreter: %s (%s)\n' \
    "$TEST_PYTHON" "$("$TEST_PYTHON" -c 'import platform; print(platform.python_version())')"
"$TEST_PYTHON" -m py_compile \
    bin/helper.py bin/send_gate.py tools/doctor.py tools/check_shared_core.py \
    tools/check_version.py tools/configure_allowlist.py \
    tools/migrate_legacy_launchagent.py
"$TEST_PYTHON" -m unittest discover -s tests -v
"$TEST_PYTHON" tools/check_shared_core.py

if [[ "$#" -eq 1 ]]; then
    "$TEST_PYTHON" tools/check_version.py "$1"
fi
