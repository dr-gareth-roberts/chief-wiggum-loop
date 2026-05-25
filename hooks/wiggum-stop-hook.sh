#!/usr/bin/env bash
# Wrapper for wiggum_stop_hook.py.
# Reads hook JSON before resolving Python so pyenv shims never receive hook stdin.
set -euo pipefail

HOOK_INPUT="$(cat)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${WIGGUM_PYTHON:-}"

if [[ -z "$PYTHON_BIN" && -x /usr/bin/python3 ]]; then
  PYTHON_BIN=/usr/bin/python3
fi

if [[ -z "$PYTHON_BIN" ]]; then
  if command -v pyenv >/dev/null 2>&1; then
    PYTHON_BIN="$(pyenv which python3 </dev/null 2>/dev/null || true)"
  fi
fi

if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN=python3
fi

printf '%s' "$HOOK_INPUT" | "$PYTHON_BIN" "$SCRIPT_DIR/wiggum_stop_hook.py"
