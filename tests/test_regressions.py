"""End-to-end regression tests that pin previously fixed bugs.

Each test spins up a tmp project, runs the real ``wiggum-isolated-loop.sh``
wrapper as a subprocess, and verifies an externally observable invariant of
the controller. The fake agents are bash scripts written via fixtures so the
controller talks to a real subprocess (same shape as the existing
``test-wiggum-isolated-loop.sh`` cases).
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path

import pytest

# --- helpers -----------------------------------------------------------------


def _git_init(project: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=str(project),
        check=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
    )


def _git_seed_commit(project: Path) -> None:
    """Create an initial commit so the repo has HEAD (required for worktrees)."""

    (project / "seed.txt").write_text("seed\n", encoding="utf-8")
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "wiggum",
        "GIT_AUTHOR_EMAIL": "wiggum@example.invalid",
        "GIT_COMMITTER_NAME": "wiggum",
        "GIT_COMMITTER_EMAIL": "wiggum@example.invalid",
    }
    subprocess.run(["git", "add", "seed.txt"], cwd=str(project), check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=str(project), check=True, env=env)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# --- tests -------------------------------------------------------------------


def test_runs_under_readonly_home(tmp_path: Path, fake_agent: Path, readonly_home: Path, isolated_run):
    """Regression: a read-only HOME must not crash the loop.

    Previous failure mode was an unhandled ``PermissionError`` when the script
    tried to create ``~/.wiggum/lessons.jsonl``. The fix made global-lesson
    writes a soft failure that records ``global_lessons_disabled`` in the
    in-project log without aborting.
    """

    if os.geteuid() == 0:
        pytest.skip("running as root would bypass chmod 555 restriction")
    if platform.system() == "Windows":
        pytest.skip("chmod 555 semantics differ on Windows")

    project = tmp_path / "project"
    project.mkdir()
    _git_init(project)
    _git_seed_commit(project)

    # Use the fake agent as a noop worker so the controller hits a stuck
    # iteration that triggers the ``append_lesson`` path (lessons are written
    # only when ``stuck_reason`` is non-empty).
    result = isolated_run(
        project,
        "NOOP_INPUT",
        "--agent-command",
        str(fake_agent),
        "--sandbox",
        "none",
        "--stuck-after",
        "0",
        "--max-iterations",
        "2",
        "--global-lessons",
        env_overrides={"HOME": str(readonly_home)},
        timeout=60,
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    # Read-only HOME means ~/.wiggum can never be created. Confirm absence.
    global_lessons = readonly_home / ".wiggum" / "lessons.jsonl"
    assert not global_lessons.exists(), "global lessons file should not exist when HOME is read-only"

    # The project-local lessons file must have been written for the stuck round.
    project_lessons = project / ".claude" / "wiggum-lessons.jsonl"
    assert project_lessons.exists(), "project-local lessons file must still be written"
    entries = _read_jsonl(project_lessons)
    assert entries, "expected at least one lesson entry for the stagnant iteration"
    assert any("stuck_reason" in e for e in entries)

    # The controller should have logged its graceful fallback at least once.
    log = _read_jsonl(project / ".claude" / "wiggum-isolated.log.jsonl")
    assert any(e.get("event") == "global_lessons_disabled" for e in log), (
        f"expected a global_lessons_disabled event in the controller log; got events: "
        f"{[e.get('event') for e in log]}"
    )


def test_acceptance_auto_respects_verifier_in_simple_invocation(
    tmp_path: Path, fake_agent: Path, isolated_run
):
    """Regression: with ``--acceptance auto`` + ``--success-command false`` +
    no metric, the resolved policy is ``verifier``. A verifier-failing
    candidate must NOT be applied to the main tree.

    The old buggy behavior accepted the patch anyway because the auto policy
    had an unsafe early-return gate. We use ``--sandbox copy`` so candidate
    writes go to an isolated workspace; applying-or-not-applying the captured
    patch is then externally observable in the main tree.
    """

    project = tmp_path / "project"
    project.mkdir()
    _git_init(project)
    _git_seed_commit(project)

    result = isolated_run(
        project,
        "make sentinel",
        "--agent-command",
        f"{fake_agent} sentinel",
        "--sandbox",
        "copy",
        "--candidates",
        "1",
        "--success-command",
        "false",
        "--acceptance",
        "auto",
        "--stuck-after",
        "0",
        "--max-iterations",
        "2",
        timeout=60,
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    # The fake "sentinel" worker tried to create sentinel.txt inside the
    # candidate sandbox. With auto→verifier and verifier exit 1, the patch
    # must not be promoted to the main tree.
    sentinel = project / "sentinel.txt"
    assert not sentinel.exists(), (
        "regression: verifier-failing candidate's sentinel leaked into the main tree; "
        "candidate_accepted under auto+verifier-false must reject."
    )

    # And the iteration log should agree: accepted=False, verifier_exit_code=1.
    log_entries = _read_jsonl(project / ".claude" / "wiggum-isolated.log.jsonl")
    iteration_events = [e for e in log_entries if e.get("event") == "iteration"]
    assert iteration_events, f"expected at least one iteration event; got {log_entries}"
    assert all(e.get("verifier_exit_code") == 1 for e in iteration_events)
    assert all(e.get("accepted") is False for e in iteration_events)


def test_classifier_prefers_stagnation_over_refusal_regex(
    tmp_path: Path, fake_agent: Path, isolated_run
):
    """Regression: classifier must check stagnation BEFORE the refusal regex.

    Before the fix, an agent that politely says "I cannot help" would be
    classified as ``model_refusal_or_give_up`` even when the real cause was
    no workspace progress. The fix re-ordered the checks so stagnation wins.
    """

    project = tmp_path / "project"
    project.mkdir()
    _git_init(project)
    _git_seed_commit(project)

    result = isolated_run(
        project,
        "anything",
        "--agent-command",
        f"{fake_agent} refusal",
        "--sandbox",
        "none",
        "--stuck-after",
        "0",
        "--max-iterations",
        "1",
        timeout=60,
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    log_entries = _read_jsonl(project / ".claude" / "wiggum-isolated.log.jsonl")
    iteration_events = [e for e in log_entries if e.get("event") == "iteration"]
    assert iteration_events, f"expected an iteration event; got {log_entries}"
    iteration = iteration_events[0]
    assert iteration["stuck_reason"] == "no_workspace_progress", (
        f"expected no_workspace_progress, got {iteration['stuck_reason']!r}; "
        f"full entry: {iteration}"
    )


def test_worktree_sandbox_carries_untracked_files_from_main(
    tmp_path: Path, isolated_run
):
    """Regression (codex P2): when `--sandbox worktree` seeds the candidate
    workspace from the main tree, untracked files in the main tree MUST be
    present inside the worktree. The earlier fix that protected the main
    tree's git index (`mark_untracked_for_diff` requires `allow_in_main=True`)
    silently dropped untracked files from the seed patch. The corrected
    behavior copies untracked files directly into the worktree.

    Strategy: the fake agent only writes `proof.txt` if it can read
    `user_new.txt` from its cwd. The verifier looks for `proof.txt`. If the
    worktree didn't inherit the untracked file, the agent can't read it, never
    writes `proof.txt`, and the verifier never passes.
    """

    project = tmp_path / "project"
    project.mkdir()
    _git_init(project)
    _git_seed_commit(project)

    # User has an untracked file in their working tree that should appear in
    # the worktree sandbox so the agent and verifier see the same project state.
    untracked = project / "user_new.txt"
    untracked.write_text("user-content\n", encoding="utf-8")

    # Conditional fake agent: only emits proof.txt if it can SEE user_new.txt
    # in its cwd. This makes "worktree saw the untracked file" externally
    # observable via the verifier rather than via prompt-side assertions.
    proof_agent = tmp_path / "proof-agent.sh"
    proof_agent.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
cat > /dev/null  # discard stdin prompt
if [[ -f user_new.txt ]] && grep -q user-content user_new.txt; then
  echo "saw the user file" > proof.txt
  echo "AGENT_SAW_USER_FILE"
else
  echo "AGENT_DID_NOT_SEE_USER_FILE"
fi
""",
        encoding="utf-8",
    )
    proof_agent.chmod(0o755)

    result = isolated_run(
        project,
        "create proof.txt if you can see user_new.txt",
        "--agent-command",
        str(proof_agent),
        "--sandbox",
        "worktree",
        "--success-command",
        "test -f proof.txt",
        "--acceptance",
        "verifier",
        "--max-iterations",
        "2",
        timeout=60,
    )

    assert result.returncode == 0, (
        f"exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    log = _read_jsonl(project / ".claude" / "wiggum-isolated.log.jsonl")
    stop_events = [e for e in log if e.get("event") == "stop"]
    assert stop_events, f"expected a stop event in {log}"
    # Stop reason should be "verifier" (verifier passed inside the worktree
    # because user_new.txt was visible there). NOT "max_iterations" (which is
    # what we'd see if the agent never managed to satisfy the verifier).
    assert stop_events[-1].get("reason") == "verifier", (
        f"regression: loop did not stop via verifier; the agent likely couldn't "
        f"see the untracked main-tree file in the worktree sandbox. "
        f"stop event: {stop_events[-1]}\nstdout:\n{result.stdout}"
    )

    # The applied patch should have promoted proof.txt to the main tree.
    assert (project / "proof.txt").exists(), (
        "regression: verifier passed but the accepted patch was not applied to main"
    )

    # The main tree's index must NOT have been mutated by the seed step.
    # `git status --porcelain user_new.txt` should still show '??' (untracked),
    # never 'A ' (added/intent-to-add).
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", "user_new.txt"],
        cwd=str(project),
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
    )
    assert status.stdout.startswith("?? "), (
        f"regression: main-tree index was mutated by candidate seeding; "
        f"git status returned: {status.stdout!r}"
    )


def test_validate_agent_commands_substitutes_prompt_file_placeholder(
    tmp_path: Path, isolated_run
):
    """Regression (codex P2): startup validation must substitute the documented
    `{prompt_file}` placeholder the same way `run_agent` does. Otherwise an
    agent command like `bash {prompt_file}` is probed with a literal
    `{prompt_file}` token and fails validation purely on substitution, aborting
    the loop before any real work runs.
    """

    project = tmp_path / "project"
    project.mkdir()
    _git_init(project)
    _git_seed_commit(project)

    # A wrapper script that REQUIRES a real prompt file path as its first arg
    # and reads it. If the placeholder isn't substituted, $1 is the literal
    # "{prompt_file}" and `cat $1` fails with a non-zero exit in well under 0.5s
    # — exactly the failure shape the startup probe flags as "missing/broken".
    wrapper = tmp_path / "needs-prompt-file.sh"
    wrapper.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
PROMPT_PATH="$1"
[[ -f "$PROMPT_PATH" ]] || { echo "no prompt file at $PROMPT_PATH" >&2; exit 1; }
cat "$PROMPT_PATH" > /dev/null
echo "ran with $(basename "$PROMPT_PATH")"
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    result = isolated_run(
        project,
        "anything",
        "--agent-command",
        f"{wrapper} {{prompt_file}}",
        "--sandbox",
        "none",
        "--stuck-after",
        "0",
        "--max-iterations",
        "1",
        timeout=60,
    )

    assert result.returncode == 0, (
        f"regression: validate_agent_commands rejected a valid {{prompt_file}} "
        f"command because the placeholder was not substituted.\n"
        f"exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "failed startup probe" not in result.stderr, (
        "validate_agent_commands should not have flagged this command"
    )


def test_state_persists_global_enabled_for_resume(
    tmp_path: Path, fake_agent: Path, isolated_run
):
    """Regression (codex P3): the runner must write `global_enabled` into
    `state` so that `/wiggum-resume` can re-apply `--global-lessons` when
    restoring a stuck archive. Otherwise an opt-in global-lessons run resumes
    as project-only, silently changing the user's persistence choice.
    """

    project = tmp_path / "project"
    project.mkdir()
    _git_init(project)
    _git_seed_commit(project)

    result = isolated_run(
        project,
        "NOOP",
        "--agent-command",
        str(fake_agent),
        "--sandbox",
        "none",
        "--stuck-after",
        "0",
        "--max-iterations",
        "1",
        "--global-lessons",
        timeout=60,
    )

    assert result.returncode == 0, (
        f"exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    archive_dir = project / ".claude" / "wiggum-archive"
    archives = sorted(archive_dir.glob("wiggum-isolated.*.json"))
    assert archives, f"expected an archive file in {archive_dir}"
    state = json.loads(archives[-1].read_text(encoding="utf-8"))
    assert state.get("global_enabled") is True, (
        f"regression: global_enabled was not persisted to state; "
        f"archive keys: {sorted(state.keys())}"
    )
