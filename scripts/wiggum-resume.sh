#!/usr/bin/env bash
# Resume the most recent stuck Wiggum isolated loop by restoring its archived
# state and replaying the original CLI invocation that produced it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${WIGGUM_PYTHON:-}"
if [[ -z "$PYTHON_BIN" && -x /usr/bin/python3 ]]; then
  PYTHON_BIN=/usr/bin/python3
fi
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN=python3
fi

# Locate the most recent stuck archive by mtime.
ARCHIVE_PATH="$(ls -t .claude/wiggum-archive/wiggum-isolated.stuck.*.json 2>/dev/null | head -1 || true)"
if [[ -z "$ARCHIVE_PATH" ]]; then
  echo "no stuck archive found under .claude/wiggum-archive/wiggum-isolated.stuck.*.json" >&2
  exit 2
fi

# Restore the archive into the active state file and emit the recomputed CLI
# args as one NUL-delimited stream so we can faithfully round-trip commands
# containing spaces (e.g. multi-token --agent-command values).
RESUME_FLAGS_FILE="$(mktemp -t wiggum-resume-flags.XXXXXX)"
trap 'rm -f "$RESUME_FLAGS_FILE"' EXIT

export WIGGUM_RESUME_ARCHIVE="$ARCHIVE_PATH"
export WIGGUM_RESUME_FLAGS_FILE="$RESUME_FLAGS_FILE"

"$PYTHON_BIN" - <<'PY'
import json
import os
import sys
from pathlib import Path

archive_path = Path(os.environ["WIGGUM_RESUME_ARCHIVE"])
flags_path = Path(os.environ["WIGGUM_RESUME_FLAGS_FILE"])

try:
    state = json.loads(archive_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    print(f"could not read archive {archive_path}: {exc}", file=sys.stderr)
    sys.exit(2)

# Restore the archive as the active state so wiggum_isolated_loop.py sees a
# previous run's bookkeeping. main() will overwrite the state file with a fresh
# dict on startup; durable continuity actually comes from the prompt, rolling
# summary, log, and lessons that remain on disk regardless.
restored = {k: v for k, v in state.items() if k not in {"stop_reason", "stopped_at"}}
restored["active"] = True
restored["restored_from_archive"] = str(archive_path)

active_state = Path(".claude/wiggum-isolated.local.json")
active_state.parent.mkdir(parents=True, exist_ok=True)
active_state.write_text(json.dumps(restored, indent=2, sort_keys=True) + "\n", encoding="utf-8")

# Reconstruct the original CLI invocation. Only forward flags whose values are
# non-default; otherwise a resumed run gets opinions it never had originally.
flags: list[str] = ["--prompt-file", state.get("prompt_path") or ".claude/wiggum-isolated-prompt.local.md"]

max_iterations = state.get("max_iterations")
if isinstance(max_iterations, int) and max_iterations > 0:
    flags.extend(["--max-iterations", str(max_iterations)])

completion_promise = state.get("completion_promise")
if completion_promise:
    flags.extend(["--completion-promise", str(completion_promise)])

success_command = state.get("success_command")
if success_command:
    flags.extend(["--success-command", str(success_command)])

metric_name = state.get("metric_name")
if metric_name:
    flags.extend(["--metric-name", str(metric_name)])
    metric_direction = state.get("metric_direction") or "higher"
    flags.extend(["--metric-direction", str(metric_direction)])

acceptance = state.get("acceptance")
if acceptance and acceptance != "auto":
    flags.extend(["--acceptance", str(acceptance)])

sandbox = state.get("sandbox")
if sandbox and sandbox != "none":
    flags.extend(["--sandbox", str(sandbox)])

mode = state.get("mode")
if mode and mode != "reflective":
    flags.extend(["--mode", str(mode)])

candidates = state.get("candidates")
if isinstance(candidates, int) and candidates > 1:
    flags.extend(["--candidates", str(candidates)])

agent_switch_every = state.get("agent_switch_every")
if isinstance(agent_switch_every, int) and agent_switch_every != 4:
    flags.extend(["--agent-switch-every", str(agent_switch_every)])

stuck_after = state.get("stuck_after")
if isinstance(stuck_after, int) and stuck_after != 3:
    flags.extend(["--stuck-after", str(stuck_after)])

for command in state.get("agent_commands") or []:
    flags.extend(["--agent-command", str(command)])

critic_command = state.get("critic_command")
if critic_command:
    flags.extend(["--critic-command", str(critic_command)])
    critic_every = state.get("critic_every") or 0
    flags.extend(["--critic-every", str(critic_every)])

review_command = state.get("review_command")
if review_command:
    flags.extend(["--review-command", str(review_command)])

# Pre-existing state may include this key from prior resumes; honor it only
# when present per the task spec.
if state.get("global_enabled"):
    flags.append("--global-lessons")

# Terminate every token with NUL (including the last) so bash `read -r -d ''`
# picks up the final element instead of dropping it.
flags_path.write_bytes(b"".join(flag.encode("utf-8") + b"\0" for flag in flags))
PY

# Read the reconstructed args back as a NUL-delimited array.
RESUME_ARGS=()
while IFS= read -r -d '' arg; do
  RESUME_ARGS+=("$arg")
done < "$RESUME_FLAGS_FILE"

echo "🔄 Resuming from $ARCHIVE_PATH"

exec "$SCRIPT_DIR/wiggum-isolated-loop.sh" "${RESUME_ARGS[@]}" "$@"
