#!/usr/bin/env bash
# Start a Wiggum Loop in the current Claude Code project.
set -euo pipefail

usage() {
  cat <<'EOF'
Wiggum Loop - progress-aware Ralph Wiggum automation

USAGE:
  /wiggum-loop [PROMPT...] [OPTIONS]

OPTIONS:
  --prompt-file <path>           Read the core prompt from a file
  --max-iterations <n>           Stop after N iterations (default: 12)
  --allow-infinite               Set max iterations to 0
  --completion-promise <text>    Stop when assistant outputs <promise>text</promise>
  --success-command <command>    Stop when this shell command exits 0
  --success-timeout <seconds>    Timeout for success command (default: 25)
  --stuck-after <n>              Pause after N no-progress stop events (default: 3; 0 disables)
  --mode <exact|reflective|variants>
                                 exact repeats only the core prompt; reflective adds steering;
                                 variants rotates anti-stall angles (default: reflective)
  -h, --help                     Show this help

EXAMPLES:
  /wiggum-loop "Fix auth and run tests" --success-command "npm test" --max-iterations 8
  /wiggum-loop --prompt-file PROMPT.md --mode variants --completion-promise DONE
EOF
}

PROMPT_PARTS=()
PROMPT_FILE=""
MAX_ITERATIONS=12
COMPLETION_PROMISE=""
SUCCESS_COMMAND=""
SUCCESS_TIMEOUT_SECONDS=25
STUCK_AFTER=3
MODE="reflective"
PYTHON_BIN="${WIGGUM_PYTHON:-}"
if [[ -z "$PYTHON_BIN" && -x /usr/bin/python3 ]]; then
  PYTHON_BIN=/usr/bin/python3
fi
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN=python3
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --prompt-file)
      [[ -n "${2:-}" ]] || { echo "--prompt-file requires a path" >&2; exit 1; }
      PROMPT_FILE="$2"
      shift 2
      ;;
    --max-iterations)
      [[ "${2:-}" =~ ^[0-9]+$ ]] || { echo "--max-iterations requires a non-negative integer" >&2; exit 1; }
      MAX_ITERATIONS="$2"
      shift 2
      ;;
    --allow-infinite)
      MAX_ITERATIONS=0
      shift
      ;;
    --completion-promise)
      [[ -n "${2:-}" ]] || { echo "--completion-promise requires text" >&2; exit 1; }
      COMPLETION_PROMISE="$2"
      shift 2
      ;;
    --success-command)
      [[ -n "${2:-}" ]] || { echo "--success-command requires a command string" >&2; exit 1; }
      SUCCESS_COMMAND="$2"
      shift 2
      ;;
    --success-timeout)
      [[ "${2:-}" =~ ^[0-9]+$ ]] || { echo "--success-timeout requires a positive integer" >&2; exit 1; }
      SUCCESS_TIMEOUT_SECONDS="$2"
      shift 2
      ;;
    --stuck-after)
      [[ "${2:-}" =~ ^[0-9]+$ ]] || { echo "--stuck-after requires a non-negative integer" >&2; exit 1; }
      STUCK_AFTER="$2"
      shift 2
      ;;
    --mode)
      [[ "${2:-}" =~ ^(exact|reflective|variants)$ ]] || { echo "--mode must be exact, reflective, or variants" >&2; exit 1; }
      MODE="$2"
      shift 2
      ;;
    --)
      shift
      while [[ $# -gt 0 ]]; do PROMPT_PARTS+=("$1"); shift; done
      ;;
    *)
      PROMPT_PARTS+=("$1")
      shift
      ;;
  esac
done

if [[ -n "$PROMPT_FILE" ]]; then
  [[ -f "$PROMPT_FILE" ]] || { echo "Prompt file not found: $PROMPT_FILE" >&2; exit 1; }
  PROMPT="$(cat "$PROMPT_FILE")"
  if [[ ${#PROMPT_PARTS[@]} -gt 0 ]]; then
    PROMPT+=$'\n\n'
    PROMPT+="${PROMPT_PARTS[*]}"
  fi
else
  PROMPT="${PROMPT_PARTS[*]:-}"
fi

if [[ -z "${PROMPT//[[:space:]]/}" ]]; then
  echo "No prompt provided. Pass prompt text or --prompt-file." >&2
  exit 1
fi

mkdir -p .claude
printf "%s\n" "$PROMPT" > .claude/wiggum-prompt.local.md

export WIGGUM_SESSION_ID="${CLAUDE_CODE_SESSION_ID:-${CLAUDE_SESSION_ID:-}}"
export WIGGUM_MAX_ITERATIONS="$MAX_ITERATIONS"
export WIGGUM_COMPLETION_PROMISE="$COMPLETION_PROMISE"
export WIGGUM_SUCCESS_COMMAND="$SUCCESS_COMMAND"
export WIGGUM_SUCCESS_TIMEOUT_SECONDS="$SUCCESS_TIMEOUT_SECONDS"
export WIGGUM_STUCK_AFTER="$STUCK_AFTER"
export WIGGUM_MODE="$MODE"

"$PYTHON_BIN" - <<'PY'
import json, os
from datetime import datetime, timezone
from pathlib import Path

def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default

state = {
    "active": True,
    "session_id": os.environ.get("WIGGUM_SESSION_ID", ""),
    "iteration": 1,
    "max_iterations": env_int("WIGGUM_MAX_ITERATIONS", 12),
    "completion_promise": os.environ.get("WIGGUM_COMPLETION_PROMISE") or None,
    "success_command": os.environ.get("WIGGUM_SUCCESS_COMMAND") or "",
    "success_timeout_seconds": env_int("WIGGUM_SUCCESS_TIMEOUT_SECONDS", 25),
    "stuck_after": env_int("WIGGUM_STUCK_AFTER", 3),
    "mode": os.environ.get("WIGGUM_MODE", "reflective"),
    "prompt_path": ".claude/wiggum-prompt.local.md",
    "last_workspace_hash": "",
    "stagnant_iterations": 0,
    "started_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
}
Path(".claude/wiggum-loop.local.json").write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
PY

cat <<EOF
🔁 Wiggum loop activated.

Mode: $MODE
Max iterations: $(if [[ "$MAX_ITERATIONS" == "0" ]]; then echo "unbounded"; else echo "$MAX_ITERATIONS"; fi)
Stuck pause: $(if [[ "$STUCK_AFTER" == "0" ]]; then echo "disabled"; else echo "after $STUCK_AFTER no-progress stop events"; fi)
Completion promise: $(if [[ -n "$COMPLETION_PROMISE" ]]; then echo "<promise>$COMPLETION_PROMISE</promise>"; else echo "none"; fi)
Success command: $(if [[ -n "$SUCCESS_COMMAND" ]]; then echo "$SUCCESS_COMMAND"; else echo "none"; fi)
State: .claude/wiggum-loop.local.json
Prompt: .claude/wiggum-prompt.local.md
Log: .claude/wiggum-loop.log.jsonl

EOF

printf "%s\n" "$PROMPT"
