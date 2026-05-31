#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"

if ! python3 -c "import pytest" 2>/dev/null; then
  echo "pytest not installed. Install with: python3 -m pip install --user pytest" >&2
  exit 1
fi

cd "$PLUGIN_ROOT"
"$HERE/test-wiggum-loop.sh"
"$HERE/test-wiggum-isolated-loop.sh"
"$HERE/test-smoke.sh"
python3 -m pytest tests/ -v --tb=short
