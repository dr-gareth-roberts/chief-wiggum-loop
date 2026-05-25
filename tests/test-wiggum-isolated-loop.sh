#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
export HOME="$TMP_ROOT/home"
mkdir -p "$HOME"
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

FAKE_AGENT="$TMP_ROOT/fake-agent.sh"
cat > "$FAKE_AGENT" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
NAME="${1:-worker}"
PROMPT="$(cat)"
mkdir -p .claude
printf '%s\n' "$NAME" >> .claude/fake-agent-names.log
printf '%s\n---END---\n' "$PROMPT" >> .claude/fake-agent-prompts.log
if [[ "$NAME" == "good" ]]; then
  echo good > result.txt
  echo "METRIC score=10"
  exit 0
fi
if [[ "$NAME" == "poor" ]]; then
  echo poor > result.txt
  echo "METRIC score=1"
  exit 0
fi
case "$PROMPT" in
  *MAKE_PASS*) touch pass.flag; echo "made pass" ;;
  *PROMISE*) echo "<promise>DONE</promise>" ;;
  *CHANGE_ONCE*) if [[ ! -f changed.txt ]]; then echo changed > changed.txt; fi; echo "changed maybe" ;;
  *) echo "noop" ;;
esac
SH
chmod +x "$FAKE_AGENT"

FAKE_CRITIC="$TMP_ROOT/fake-critic.sh"
cat > "$FAKE_CRITIC" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
INPUT="$(cat)"
mkdir -p .claude
printf '%s\n---CRITIC-END---\n' "$INPUT" >> .claude/fake-critic-inputs.log
echo "CRITIC: do not repeat the previous no-op; target verifier output."
SH
chmod +x "$FAKE_CRITIC"

FAKE_REVIEWER="$TMP_ROOT/fake-reviewer.sh"
cat > "$FAKE_REVIEWER" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
INPUT="$(cat)"
mkdir -p .claude
printf '%s\n---REVIEW-END---\n' "$INPUT" >> .claude/fake-reviewer-inputs.log
echo "Looks acceptable. <review>APPROVED</review>"
SH
chmod +x "$FAKE_REVIEWER"

# 1. Success command stops after a fresh isolated agent process creates the flag.
CASE1="$TMP_ROOT/success"
mkdir -p "$CASE1"
(
  cd "$CASE1"
  git init -q
  WIGGUM_PYTHON="$TEST_PYTHON" "$PLUGIN_ROOT/scripts/wiggum-isolated-loop.sh" \
    "MAKE_PASS" \
    --agent-command "$FAKE_AGENT" \
    --success-command "test -f pass.flag" \
    --max-iterations 3 > out.txt
)
[[ -f "$CASE1/pass.flag" ]] || fail "fake agent did not create pass.flag"
grep -q "Success command passed" "$CASE1/out.txt" || fail "success command did not stop isolated loop"
grep -q "fresh isolated agent invocation" "$CASE1/.claude/fake-agent-prompts.log" || fail "isolated prompt marker missing"
find "$CASE1/.claude/wiggum-archive" -name 'wiggum-isolated.verifier.*.json' | grep -q . || fail "verifier archive missing"

# 2. Completion promise stops without verifier.
CASE2="$TMP_ROOT/promise"
mkdir -p "$CASE2"
(
  cd "$CASE2"
  git init -q
  WIGGUM_PYTHON="$TEST_PYTHON" "$PLUGIN_ROOT/scripts/wiggum-isolated-loop.sh" \
    "PROMISE" \
    --agent-command "$FAKE_AGENT" \
    --completion-promise DONE \
    --max-iterations 3 > out.txt
)
grep -q "Completion promise detected" "$CASE2/out.txt" || fail "promise did not stop isolated loop"
find "$CASE2/.claude/wiggum-archive" -name 'wiggum-isolated.promise.*.json' | grep -q . || fail "promise archive missing"

# 3. No-progress pause works because Wiggum bookkeeping is excluded from the workspace hash.
CASE3="$TMP_ROOT/stuck"
mkdir -p "$CASE3"
(
  cd "$CASE3"
  git init -q
  WIGGUM_PYTHON="$TEST_PYTHON" "$PLUGIN_ROOT/scripts/wiggum-isolated-loop.sh" \
    "NOOP" \
    --agent-command "$FAKE_AGENT" \
    --stuck-after 1 \
    --max-iterations 5 > out.txt
)
grep -q "Paused after 1 no-progress" "$CASE3/out.txt" || fail "no-progress pause did not trigger"
find "$CASE3/.claude/wiggum-archive" -name 'wiggum-isolated.stuck.*.json' | grep -q . || fail "stuck archive missing"

# 4. Max iterations stops when progress happens once but no verifier/promise completes it.
CASE4="$TMP_ROOT/max"
mkdir -p "$CASE4"
(
  cd "$CASE4"
  git init -q
  WIGGUM_PYTHON="$TEST_PYTHON" "$PLUGIN_ROOT/scripts/wiggum-isolated-loop.sh" \
    "CHANGE_ONCE" \
    --agent-command "$FAKE_AGENT" \
    --stuck-after 0 \
    --max-iterations 2 > out.txt
)
grep -q "Max iterations (2) reached" "$CASE4/out.txt" || fail "max iterations did not stop isolated loop"
[[ "$(grep -c -- '---END---' "$CASE4/.claude/fake-agent-prompts.log")" == "2" ]] || fail "expected exactly two isolated agent prompts"
grep -q "Prior rounds summary" "$CASE4/.claude/fake-agent-prompts.log" || fail "second isolated prompt did not include rolling summary"
grep -q "Iteration 1" "$CASE4/.claude/wiggum-isolated-summary.local.md" || fail "rolling summary did not record first iteration"

# 5. Multiple worker commands rotate by batch size, and critic runs after each batch.
CASE5="$TMP_ROOT/multimodel"
mkdir -p "$CASE5"
(
  cd "$CASE5"
  git init -q
  WIGGUM_PYTHON="$TEST_PYTHON" "$PLUGIN_ROOT/scripts/wiggum-isolated-loop.sh" \
    "NOOP" \
    --agent-command "$FAKE_AGENT alpha" \
    --agent-command "$FAKE_AGENT beta" \
    --agent-switch-every 2 \
    --critic-command "$FAKE_CRITIC" \
    --critic-every 2 \
    --stuck-after 0 \
    --max-iterations 4 > out.txt
)
printf 'alpha\nalpha\nbeta\nbeta\n' > "$CASE5/expected-names.log"
diff -u "$CASE5/expected-names.log" "$CASE5/.claude/fake-agent-names.log" || fail "agent batch rotation was wrong"
grep -q "CRITIC: do not repeat" "$CASE5/.claude/wiggum-isolated-summary.local.md" || fail "critic output was not folded into summary"
[[ "$(grep -c -- '---CRITIC-END---' "$CASE5/.claude/fake-critic-inputs.log")" == "2" ]] || fail "critic did not run after each batch"

# 6. Final reviewer runs before verifier success exit.
CASE6="$TMP_ROOT/reviewer"
mkdir -p "$CASE6"
(
  cd "$CASE6"
  git init -q
  WIGGUM_PYTHON="$TEST_PYTHON" "$PLUGIN_ROOT/scripts/wiggum-isolated-loop.sh" \
    "MAKE_PASS" \
    --agent-command "$FAKE_AGENT" \
    --success-command "test -f pass.flag" \
    --review-command "$FAKE_REVIEWER" \
    --max-iterations 3 > out.txt
)
grep -q "Final review approved" "$CASE6/out.txt" || fail "final review approval was not reported"
grep -q "final_reviewer" "$CASE6/.claude/fake-reviewer-inputs.log" || fail "reviewer did not receive final review payload"
grep -q "Final review for verifier" "$CASE6/.claude/wiggum-isolated-summary.local.md" || fail "review output was not written to summary"

# 7. Best-of-N + metric acceptance in worktree sandbox keeps the best patch only.
CASE7="$TMP_ROOT/worktree-metric"
mkdir -p "$CASE7"
(
  cd "$CASE7"
  git init -q
  git config user.email test@example.com
  git config user.name Test
  echo base > base.txt
  git add base.txt
  git commit -q -m base
  WIGGUM_PYTHON="$TEST_PYTHON" "$PLUGIN_ROOT/scripts/wiggum-isolated-loop.sh" \
    "Optimize score" \
    --agent-command "$FAKE_AGENT poor" \
    --agent-command "$FAKE_AGENT good" \
    --candidates 2 \
    --candidate-concurrency 2 \
    --sandbox worktree \
    --acceptance metric \
    --metric-name score \
    --metric-direction higher \
    --stuck-after 0 \
    --max-iterations 1 > out.txt
)
[[ "$(cat "$CASE7/result.txt")" == "good" ]] || fail "best metric candidate patch was not applied"
if grep -q poor "$CASE7/result.txt"; then fail "poor candidate leaked into main tree"; fi
grep -q "metric=10" "$CASE7/out.txt" || fail "best metric was not reported"

# 8. Human checkpoint can pause the loop with a reviewable checkpoint file.
CASE8="$TMP_ROOT/human-checkpoint"
mkdir -p "$CASE8"
(
  cd "$CASE8"
  git init -q
  WIGGUM_PYTHON="$TEST_PYTHON" "$PLUGIN_ROOT/scripts/wiggum-isolated-loop.sh" \
    "NOOP" \
    --agent-command "$FAKE_AGENT" \
    --human-checkpoint always \
    --human-checkpoint-every 1 \
    --stuck-after 0 \
    --max-iterations 3 > out.txt
)
grep -q "Human checkpoint" "$CASE8/out.txt" || fail "human checkpoint was not reported"
[[ -f "$CASE8/.claude/wiggum-checkpoint.local.md" ]] || fail "checkpoint file missing"

printf 'All Wiggum isolated-loop tests passed.\n'
