#!/usr/bin/env bash
# Run Wiggum as true isolated subprocess invocations.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${WIGGUM_PYTHON:-}"
if [[ -z "$PYTHON_BIN" && -x /usr/bin/python3 ]]; then
  PYTHON_BIN=/usr/bin/python3
fi
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN=python3
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/wiggum_isolated_loop.py" "$@"
