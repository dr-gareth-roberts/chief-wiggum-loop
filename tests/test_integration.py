"""End-to-end integration tests for the controller entry points.

These complement the pure-helper unit suite by driving ``main()`` of the
isolated runner (via the real shell wrapper) and the Stop-hook script as a
subprocess, asserting externally observable behavior.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
STOP_HOOK = PLUGIN_ROOT / "hooks" / "wiggum_stop_hook.py"

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "wiggum",
    "GIT_AUTHOR_EMAIL": "wiggum@example.invalid",
    "GIT_COMMITTER_NAME": "wiggum",
    "GIT_COMMITTER_EMAIL": "wiggum@example.invalid",
}


def _git(project: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(project), check=True, env={**os.environ, **GIT_ENV})


def _git_repo(project: Path) -> None:
    _git(project, "init", "-q", "-b", "main")
    (project / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(project, "add", "seed.txt")
    _git(project, "commit", "-q", "-m", "seed")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


# --- isolated runner main() --------------------------------------------------


def test_isolated_loop_stops_on_completion_promise(tmp_path: Path, fake_agent: Path, isolated_run):
    """A worker that emits the promise tag drives main() to a clean terminal stop."""

    project = tmp_path / "project"
    project.mkdir()
    _git_repo(project)

    result = isolated_run(
        project,
        "PROMISE please",
        "--agent-command",
        str(fake_agent),
        "--completion-promise",
        "DONE",
        "--sandbox",
        "none",
        "--max-iterations",
        "5",
        timeout=60,
    )

    assert result.returncode == 0, f"exit={result.returncode}\n{result.stdout}\n{result.stderr}"
    log = _read_jsonl(project / ".claude" / "wiggum-isolated.log.jsonl")
    stops = [e for e in log if e.get("event") == "stop"]
    assert stops and stops[-1].get("reason") == "promise", f"expected promise stop, got {log}"


def test_best_of_n_worktree_concurrency_applies_and_cleans_up(tmp_path: Path, fake_agent: Path, isolated_run):
    """Concurrent best-of-N over worktrees applies a candidate and leaves no
    dangling worktrees (exercises the worktree lock + shared baseline path)."""

    project = tmp_path / "project"
    project.mkdir()
    _git_repo(project)

    result = isolated_run(
        project,
        "CHANGE_ONCE now",
        "--agent-command",
        str(fake_agent),
        "--sandbox",
        "worktree",
        "--candidates",
        "3",
        "--candidate-concurrency",
        "3",
        "--acceptance",
        "always",
        "--stuck-after",
        "0",
        "--max-iterations",
        "1",
        timeout=90,
    )

    assert result.returncode == 0, f"exit={result.returncode}\n{result.stdout}\n{result.stderr}"
    # The accepted candidate's new file must land in the main tree.
    assert (project / "changed.txt").exists(), "best candidate patch was not applied to the main tree"
    # All temporary worktrees must be cleaned up: only the main worktree remains.
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(project),
        env={**os.environ, **GIT_ENV},
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout
    worktree_count = sum(1 for line in listing.splitlines() if line.startswith("worktree "))
    assert worktree_count == 1, f"expected only the main worktree to remain, saw:\n{listing}"


# --- stop hook main() --------------------------------------------------------


def _run_stop_hook(project: Path, hook_input: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(STOP_HOOK)],
        cwd=str(project),
        input=json.dumps(hook_input),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )


def test_stop_hook_blocks_and_reinjects_prompt(tmp_path: Path):
    """With an active state and no terminal condition, the hook emits a
    ``decision: block`` payload that re-injects the core prompt."""

    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    state = {
        "active": True,
        "session_id": "",
        "iteration": 1,
        "max_iterations": 5,
        "stuck_after": 0,
        "mode": "reflective",
        "prompt": "Improve the widget",
        "completion_promise": None,
        "success_command": "",
        "last_workspace_hash": "",
        "stagnant_iterations": 0,
    }
    (project / ".claude" / "wiggum-loop.local.json").write_text(json.dumps(state), encoding="utf-8")

    result = _run_stop_hook(project, {"session_id": "", "transcript_path": ""})
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    assert "Improve the widget" in payload["reason"]
    assert "Wiggum loop iteration 2" in payload["systemMessage"]

    # State must have advanced to the next iteration and logged a continue event.
    new_state = json.loads((project / ".claude" / "wiggum-loop.local.json").read_text(encoding="utf-8"))
    assert new_state["iteration"] == 2
    log = _read_jsonl(project / ".claude" / "wiggum-loop.log.jsonl")
    assert any(e.get("event") == "continue" for e in log)


def test_stop_hook_noops_without_state(tmp_path: Path):
    """No state file means the hook exits 0 and stays silent (loop inactive)."""

    result = _run_stop_hook(tmp_path, {"session_id": "", "transcript_path": ""})
    assert result.returncode == 0
    assert result.stdout.strip() == ""
