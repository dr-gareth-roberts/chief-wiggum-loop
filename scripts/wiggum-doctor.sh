#!/usr/bin/env bash
# Wiggum preflight diagnostics: verify the environment can run an isolated loop.
set -uo pipefail

PYTHON_BIN="${WIGGUM_PYTHON:-}"
if [[ -z "$PYTHON_BIN" && -x /usr/bin/python3 ]]; then
  PYTHON_BIN=/usr/bin/python3
fi
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN=python3
fi

# Probe the Claude CLI outside Python so we can use the shell's own timeout
# behavior without depending on a particular Python timeout shape.
CLAUDE_PROBE_RESULT="unknown"
if command -v claude >/dev/null 2>&1; then
  # `claude --print --help` should exit 0 quickly; cap it at 5 seconds.
  if claude --print --help </dev/null >/dev/null 2>&1 &
  then
    probe_pid=$!
    waited=0
    while kill -0 "$probe_pid" 2>/dev/null; do
      if (( waited >= 5 )); then
        kill "$probe_pid" 2>/dev/null || true
        wait "$probe_pid" 2>/dev/null || true
        CLAUDE_PROBE_RESULT="timeout"
        break
      fi
      sleep 1
      waited=$((waited + 1))
    done
    if [[ "$CLAUDE_PROBE_RESULT" != "timeout" ]]; then
      if wait "$probe_pid"; then
        CLAUDE_PROBE_RESULT="ok"
      else
        CLAUDE_PROBE_RESULT="nonzero"
      fi
    fi
  else
    CLAUDE_PROBE_RESULT="spawn_failed"
  fi
else
  CLAUDE_PROBE_RESULT="missing"
fi

export CLAUDE_PROBE_RESULT

"$PYTHON_BIN" - <<'PY'
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

failures = 0
warnings = 0
checks = 0


def ok(name: str, detail: str) -> None:
    global checks
    checks += 1
    print(f"✅ {name}: {detail}")


def bad(name: str, detail: str) -> None:
    global checks, failures
    checks += 1
    failures += 1
    print(f"❌ {name}: {detail}")


def warn(name: str, detail: str) -> None:
    global checks, warnings
    checks += 1
    warnings += 1
    print(f"⚠️ {name}: {detail}")


# 1. Python version + binary
py_version = ".".join(str(part) for part in sys.version_info[:3])
if sys.version_info >= (3, 9):
    ok("python", f"{py_version} at {sys.executable}")
else:
    bad("python", f"{py_version} at {sys.executable} (need >= 3.9)")

# 2. Git repo presence
cwd = Path.cwd()
git_dir_present = (cwd / ".git").exists()
in_worktree = False
if git_dir_present:
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=8,
    )
    in_worktree = probe.returncode == 0 and probe.stdout.strip() == b"true"
if git_dir_present and in_worktree:
    ok("git repo", f"{cwd} is a git work tree")
else:
    detail = "no .git directory found" if not git_dir_present else "git rev-parse failed"
    bad("git repo", f"{cwd}: {detail}")

# 3. HEAD presence
if git_dir_present and in_worktree:
    head_probe = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=8,
    )
    if head_probe.returncode == 0:
        ok("git HEAD", head_probe.stdout.decode("utf-8", errors="replace").strip())
    else:
        bad("git HEAD", "no commits yet (worktree-sandbox mode requires HEAD)")
else:
    bad("git HEAD", "skipped (not a git repo)")

# 4. ~/.wiggum/ writable probe (failure here is informational, not fatal)
wiggum_dir = Path.home() / ".wiggum"
try:
    wiggum_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=wiggum_dir, prefix="doctor-probe-", delete=True):
        pass
    ok("~/.wiggum writable", str(wiggum_dir))
except (OSError, PermissionError) as exc:
    warn(
        "~/.wiggum writable",
        f"{wiggum_dir}: {exc}; leaving --global-lessons off is fine (global lessons are now opt-in by default)",
    )

# 5. Env vars (warnings only when unset)
env_vars = [
    "WIGGUM_AGENT_COMMAND",
    "WIGGUM_PRIMARY_AGENT",
    "WIGGUM_SECONDARY_AGENT",
    "WIGGUM_CRITIC_COMMAND",
    "WIGGUM_REVIEW_COMMAND",
]
for name in env_vars:
    value = os.environ.get(name)
    if value:
        ok(f"env {name}", value)
    else:
        warn(f"env {name}", "unset (default fallbacks will be used)")

# 6. Claude CLI probe — shell script populated CLAUDE_PROBE_RESULT.
probe_result = os.environ.get("CLAUDE_PROBE_RESULT", "unknown")
if probe_result == "ok":
    ok("claude CLI", "claude --print --help exited 0 within 5s")
elif probe_result == "missing":
    bad("claude CLI", "claude binary not found on PATH (install Claude Code CLI)")
elif probe_result == "timeout":
    bad("claude CLI", "claude --print --help did not return within 5s (install/repair Claude Code CLI)")
elif probe_result == "nonzero":
    bad("claude CLI", "claude --print --help exited non-zero (install/repair Claude Code CLI)")
else:
    bad("claude CLI", f"probe failed ({probe_result})")

# 7. Active loop state — informational
state_files = [
    Path(".claude/wiggum-isolated.local.json"),
    Path(".claude/wiggum-loop.local.json"),
]
active = [str(p) for p in state_files if (cwd / p).exists()]
if active:
    ok("active loop state", "; ".join(active))
else:
    ok("active loop state", "none")

# 8. Recent stuck archives — informational count
archive_dir = cwd / ".claude" / "wiggum-archive"
if archive_dir.exists():
    stuck_archives = sorted(archive_dir.glob("wiggum-isolated.stuck.*.json"))
    if stuck_archives:
        ok("stuck archives", f"{len(stuck_archives)} present in {archive_dir} (use /wiggum-resume to continue)")
    else:
        ok("stuck archives", "none")
else:
    ok("stuck archives", "none")

print()
print(f"Summary: {checks} checks, {failures} failed, {warnings} warnings")
sys.exit(0 if failures == 0 else 1)
PY
