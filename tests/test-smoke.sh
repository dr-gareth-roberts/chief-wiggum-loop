#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
TEST_PYTHON="${WIGGUM_PYTHON:-}"
if [[ -z "$TEST_PYTHON" && -x /usr/bin/python3 ]]; then
  TEST_PYTHON=/usr/bin/python3
fi
if [[ -z "$TEST_PYTHON" || ! -x "$TEST_PYTHON" ]]; then
  TEST_PYTHON="$(command -v python3)"
fi
trap 'rm -rf "$TMP_ROOT"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

FAKE_BIN="$TMP_ROOT/bin"
mkdir -p "$FAKE_BIN"

FAKE_CLAUDE="$FAKE_BIN/claude"
cat > "$FAKE_CLAUDE" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--print" && "${2:-}" == "--help" ]]; then
  echo "fake claude help"
  exit 0
fi
cat >/dev/null || true
echo "fake claude"
SH
chmod +x "$FAKE_CLAUDE"

FAKE_OSASCRIPT="$FAKE_BIN/osascript"
cat > "$FAKE_OSASCRIPT" <<'SH'
#!/usr/bin/env bash
echo "osascript unavailable" >&2
exit 127
SH
chmod +x "$FAKE_OSASCRIPT"

FAKE_AGENT="$TMP_ROOT/fake-agent.sh"
cat > "$FAKE_AGENT" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
cat >/dev/null
echo "smoke noop"
SH
chmod +x "$FAKE_AGENT"

# 1. Doctor preflight runs cleanly and prints the expected checks.
DOCTOR_CASE="$TMP_ROOT/doctor"
mkdir -p "$DOCTOR_CASE/home" "$DOCTOR_CASE/project"
(
  cd "$DOCTOR_CASE/project"
  git init -q
  git config user.email test@example.com
  git config user.name Test
  echo seed > seed.txt
  git add seed.txt
  git commit -q -m seed
  HOME="$DOCTOR_CASE/home" PATH="$FAKE_BIN:$PATH" WIGGUM_PYTHON="$TEST_PYTHON" \
    "$PLUGIN_ROOT/scripts/wiggum-doctor.sh" > out.txt
)
grep -q "python:" "$DOCTOR_CASE/project/out.txt" || fail "doctor output missing python check"
grep -q "git repo:" "$DOCTOR_CASE/project/out.txt" || fail "doctor output missing git repo check"
grep -q "git HEAD:" "$DOCTOR_CASE/project/out.txt" || fail "doctor output missing git HEAD check"
grep -q "claude CLI:" "$DOCTOR_CASE/project/out.txt" || fail "doctor output missing claude CLI check"
grep -q "Summary:" "$DOCTOR_CASE/project/out.txt" || fail "doctor output missing summary"

# 2. Resume entrypoint reports a controlled no-archive no-op instead of crashing.
RESUME_CASE="$TMP_ROOT/resume"
mkdir -p "$RESUME_CASE"
set +e
(
  cd "$RESUME_CASE"
  WIGGUM_PYTHON="$TEST_PYTHON" /bin/bash "$PLUGIN_ROOT/scripts/wiggum-resume.sh" \
    > out.txt 2> err.txt
)
resume_status=$?
set -e
[[ "$resume_status" == "2" ]] || fail "resume no-archive path should exit 2, got $resume_status"
grep -q "no stuck archive found" "$RESUME_CASE/err.txt" || fail "resume no-archive message missing"

# 3. --notify is best-effort: missing osascript must not crash or change exit status.
NOTIFY_CASE="$TMP_ROOT/notify"
mkdir -p "$NOTIFY_CASE"
(
  cd "$NOTIFY_CASE"
  PATH="$FAKE_BIN:$PATH" WIGGUM_PYTHON="$TEST_PYTHON" \
    /bin/bash "$PLUGIN_ROOT/scripts/wiggum-isolated-loop.sh" \
    "NOOP" \
    --agent-command "$FAKE_AGENT" \
    --no-agent-validation \
    --sandbox none \
    --stuck-after 0 \
    --max-iterations 1 \
    --notify > out.txt 2> err.txt
)
grep -q "Max iterations (1) reached" "$NOTIFY_CASE/out.txt" || fail "--notify smoke did not reach terminal state"
if grep -qi "osascript" "$NOTIFY_CASE/err.txt"; then
  fail "--notify leaked missing osascript error"
fi

printf 'All Wiggum smoke tests passed.\n'
