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
import json
from pathlib import Path

CONFIGS = [
    (Path('.claude/wiggum-loop.local.json'), Path('.claude/wiggum-loop.log.jsonl'), 'Stop-hook Wiggum loop'),
    (Path('.claude/wiggum-isolated.local.json'), Path('.claude/wiggum-isolated.log.jsonl'), 'Isolated Wiggum loop'),
]

found = False
for state_path, log_path, label in CONFIGS:
    if not state_path.exists():
        continue
    found = True
    state = json.loads(state_path.read_text())
    print(label)
    print(f"  iteration: {state.get('iteration')}")
    max_i = state.get('max_iterations')
    print(f"  max_iterations: {'unbounded' if max_i == 0 else max_i}")
    for key in ['mode', 'preset', 'sandbox', 'acceptance', 'candidates', 'stuck_after', 'stagnant_iterations', 'best_metric', 'agent_runs', 'estimated_tokens']:
        if key in state:
            print(f"  {key}: {state.get(key)}")
    print(f"  completion_promise: {state.get('completion_promise') or 'none'}")
    print(f"  success_command: {state.get('success_command') or 'none'}")
    if state.get('agent_commands'):
        print('  agent_commands:')
        for command in state['agent_commands']:
            print(f"    - {command}")
    if log_path.exists():
        print('  recent ledger entries:')
        for line in log_path.read_text().splitlines()[-5:]:
            try:
                obj = json.loads(line)
                print(f"    {obj.get('timestamp')} {obj.get('event')} iteration={obj.get('iteration')} reason={obj.get('reason', '')} stuck={obj.get('stuck_reason', '')}")
            except Exception:
                print(f"    {line[:160]}")
    print()

if not found:
    print('No active Wiggum loop found.')

for path in [Path('.claude/wiggum-dashboard.md'), Path('.claude/wiggum-isolated-summary.local.md'), Path('.claude/wiggum-checkpoint.local.md')]:
    if path.exists():
        print(f"Artifact: {path}")
PY
