#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
TEST_PYTHON="${WIGGUM_PYTHON:-}"
if [[ -z "$TEST_PYTHON" && -x /usr/bin/python3 ]]; then
  TEST_PYTHON=/usr/bin/python3
fi
if [[ -z "$TEST_PYTHON" || ! -x "$TEST_PYTHON" ]]; then
  TEST_PYTHON=python3
fi
trap 'rm -rf "$TMP_ROOT"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

json_get_iteration() {
  "$TEST_PYTHON" -c 'import json, sys; print(json.load(open(sys.argv[1]))["iteration"])' "$1"
}

make_transcript() {
  local path="$1"
  local text="$2"
  "$TEST_PYTHON" - "$path" "$text" <<'PY'
import json, sys
path, text = sys.argv[1], sys.argv[2]
obj = {"message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
open(path, "w").write(json.dumps(obj) + "\n")
PY
}

run_hook() {
  local cwd="$1"
  local transcript="$2"
  local session="${3:-test-session}"
  "$TEST_PYTHON" -c 'import json, sys; print(json.dumps({"session_id": sys.argv[2], "transcript_path": sys.argv[1]}))' "$transcript" "$session" \
    | (cd "$cwd" && WIGGUM_PYTHON="$TEST_PYTHON" bash "$PLUGIN_ROOT/hooks/wiggum-stop-hook.sh")
}

# 1. No state exits silently.
NO_STATE_DIR="$TMP_ROOT/no-state"
mkdir -p "$NO_STATE_DIR"
OUT="$(run_hook "$NO_STATE_DIR" "$NO_STATE_DIR/transcript.jsonl" || true)"
[[ -z "$OUT" ]] || fail "no-state hook should be silent"

# 2. Setup creates JSON state and prompt.
CASE1="$TMP_ROOT/case1"
mkdir -p "$CASE1"
(
  cd "$CASE1"
  WIGGUM_PYTHON="$TEST_PYTHON" CLAUDE_CODE_SESSION_ID=test-session "$PLUGIN_ROOT/scripts/setup-wiggum-loop.sh" "Improve the thing" --max-iterations 3 --completion-promise DONE >/tmp/wiggum-setup.out
)
[[ -f "$CASE1/.claude/wiggum-loop.local.json" ]] || fail "state file missing"
[[ -f "$CASE1/.claude/wiggum-prompt.local.md" ]] || fail "prompt file missing"
"$TEST_PYTHON" - "$CASE1/.claude/wiggum-loop.local.json" <<'PY' || fail "state JSON invalid"
import json, sys
s=json.load(open(sys.argv[1]))
assert s["iteration"] == 1
assert s["max_iterations"] == 3
assert s["completion_promise"] == "DONE"
PY

# 3. Session mismatch ignores the state.
make_transcript "$CASE1/transcript.jsonl" "not done"
OUT="$(run_hook "$CASE1" "$CASE1/transcript.jsonl" other-session)"
[[ -z "$OUT" ]] || fail "session mismatch should be silent"
ITER="$(json_get_iteration "$CASE1/.claude/wiggum-loop.local.json")"
[[ "$ITER" == "1" ]] || fail "session mismatch changed iteration"

# 4. Normal Stop blocks and increments.
OUT="$(run_hook "$CASE1" "$CASE1/transcript.jsonl" test-session)"
"$TEST_PYTHON" - "$OUT" <<'PY' || fail "block output was not valid JSON"
import json, sys
obj=json.loads(sys.argv[1])
assert obj["decision"] == "block"
assert "Improve the thing" in obj["reason"]
assert "Wiggum loop iteration 2" in obj["systemMessage"]
PY
ITER="$(json_get_iteration "$CASE1/.claude/wiggum-loop.local.json")"
[[ "$ITER" == "2" ]] || fail "iteration did not increment"

# 5. Promise stops and archives.
make_transcript "$CASE1/transcript.jsonl" "All set. <promise>DONE</promise>"
OUT="$(run_hook "$CASE1" "$CASE1/transcript.jsonl" test-session)"
[[ "$OUT" == *"detected completion promise"* ]] || fail "promise did not stop"
[[ ! -f "$CASE1/.claude/wiggum-loop.local.json" ]] || fail "state should be archived after promise"
find "$CASE1/.claude/wiggum-archive" -name 'wiggum-loop.promise.*.json' | grep -q . || fail "promise archive missing"

# 6. Success command stops without promise.
CASE2="$TMP_ROOT/case2"
mkdir -p "$CASE2"
(
  cd "$CASE2"
  git init -q
  WIGGUM_PYTHON="$TEST_PYTHON" CLAUDE_CODE_SESSION_ID=test-session "$PLUGIN_ROOT/scripts/setup-wiggum-loop.sh" "Make checks pass" --success-command "test -f pass.flag" --max-iterations 5 >/tmp/wiggum-setup2.out
  touch pass.flag
)
make_transcript "$CASE2/transcript.jsonl" "working"
OUT="$(run_hook "$CASE2" "$CASE2/transcript.jsonl" test-session)"
[[ "$OUT" == *"success command passed"* ]] || fail "success command did not stop"
[[ ! -f "$CASE2/.claude/wiggum-loop.local.json" ]] || fail "state should archive after success command"

# 7. No-progress pause after repeated unchanged workspace hash.
CASE3="$TMP_ROOT/case3"
mkdir -p "$CASE3"
(
  cd "$CASE3"
  git init -q
  WIGGUM_PYTHON="$TEST_PYTHON" CLAUDE_CODE_SESSION_ID=test-session "$PLUGIN_ROOT/scripts/setup-wiggum-loop.sh" "Keep improving" --stuck-after 1 --max-iterations 5 >/tmp/wiggum-setup3.out
)
make_transcript "$CASE3/transcript.jsonl" "not done"
OUT1="$(run_hook "$CASE3" "$CASE3/transcript.jsonl" test-session)"
[[ "$OUT1" == *'"decision": "block"'* ]] || fail "first no-progress run should continue"
OUT2="$(run_hook "$CASE3" "$CASE3/transcript.jsonl" test-session)"
[[ "$OUT2" == *"paused after 1 no-progress"* ]] || fail "second no-progress run should pause"
[[ ! -f "$CASE3/.claude/wiggum-loop.local.json" ]] || fail "state should archive after stuck pause"

# 8. Max iterations stops cleanly.
CASE4="$TMP_ROOT/case4"
mkdir -p "$CASE4"
(
  cd "$CASE4"
  WIGGUM_PYTHON="$TEST_PYTHON" CLAUDE_CODE_SESSION_ID=test-session "$PLUGIN_ROOT/scripts/setup-wiggum-loop.sh" "One shot" --max-iterations 1 >/tmp/wiggum-setup4.out
)
make_transcript "$CASE4/transcript.jsonl" "not done"
OUT="$(run_hook "$CASE4" "$CASE4/transcript.jsonl" test-session)"
[[ "$OUT" == *"max iterations"* ]] || fail "max iterations did not stop"

printf 'All Wiggum hook tests passed.\n'
