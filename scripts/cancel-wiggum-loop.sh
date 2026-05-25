#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${WIGGUM_PYTHON:-}"
if [[ -z "$PYTHON_BIN" && -x /usr/bin/python3 ]]; then
  PYTHON_BIN=/usr/bin/python3
fi
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN=python3
fi

"$PYTHON_BIN" - <<'PY'
import json, shutil
from datetime import datetime, timezone
from pathlib import Path

state_path = Path('.claude/wiggum-loop.local.json')
if not state_path.exists():
    print('No active Wiggum loop found.')
    raise SystemExit(0)

try:
    state = json.loads(state_path.read_text())
    iteration = state.get('iteration', '?')
except Exception:
    state = {}
    iteration = '?'

archive_dir = Path('.claude/wiggum-archive')
archive_dir.mkdir(parents=True, exist_ok=True)
stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z').replace(':', '').replace('-', '')
archive = archive_dir / f'wiggum-loop.cancelled.{stamp}.json'
state.update({'active': False, 'stopped_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z'), 'stop_reason': 'cancelled'})
state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n')
shutil.move(str(state_path), str(archive))
print(f'Cancelled Wiggum loop at iteration {iteration}; archived state at {archive}')
PY
