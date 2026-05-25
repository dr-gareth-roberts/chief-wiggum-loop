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
import stat
import subprocess
import sys
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
