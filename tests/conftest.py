"""Test harness setup for pytest.

Puts the plugin's ``scripts/`` directory on ``sys.path`` so we can import
``wiggum_isolated_loop`` (the snake_case Python module backing the
``wiggum-isolated-loop.sh`` wrapper) and exercise its pure helpers directly.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import wiggum_isolated_loop as wil  # noqa: E402  (path manipulation must come first)


@pytest.fixture
def wil_module():
    """Expose the imported module and reset its mutable module-level state.

    ``append_lesson`` keeps a one-shot warning flag (``_GLOBAL_LESSONS_WARNED``);
    if we don't reset it between tests, test order leaks behavior between cases.
    """

    wil._GLOBAL_LESSONS_WARNED = False
    yield wil
    wil._GLOBAL_LESSONS_WARNED = False


def _make_args(**overrides) -> argparse.Namespace:
    """Build a minimal ``argparse.Namespace`` for the pure helpers.

    We avoid going through ``parse_args`` because it inspects ``Path.cwd()`` and
    can auto-upgrade ``--sandbox`` to ``worktree`` when run from inside this
    repo, which contaminates assertions about other fields.
    """

    defaults: dict = {
        "agent_command": ["claude --print"],
        "agent_switch_every": 4,
        "agent_timeout": 600,
        "agent_retries": 1,
        "max_iterations": 12,
        "completion_promise": None,
        "success_command": "",
        "success_timeout": 60,
        "metric_name": "",
        "metric_direction": "higher",
        "acceptance": "auto",
        "sandbox": "none",
        "candidates": 1,
        "candidate_concurrency": 1,
        "stuck_after": 3,
        "prompt_mutation": "deterministic",
        "mutation_command": "",
        "mode": "reflective",
        "memory_mode": "summary",
        "summary_command": "",
        "summary_timeout": 120,
        "summary_max_chars": 6000,
        "critic_command": "",
        "critic_every": 0,
        "critic_timeout": 300,
        "review_command": "",
        "review_timeout": 300,
        "review_approval_token": "APPROVED",
        "max_runtime_seconds": 0,
        "max_agent_runs": 0,
        "max_estimated_tokens": 0,
        "human_checkpoint": "never",
        "human_checkpoint_every": 0,
        "global_lessons": False,
        "no_global_lessons": False,
        "no_agent_validation": True,
        "explain": False,
        "dry_run": False,
        "prompt_file": None,
        "preset": "none",
        "allow_infinite": False,
        "prompt": [],
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.fixture
def make_args():
    """Return a factory for ``argparse.Namespace`` instances."""

    return _make_args


@pytest.fixture
def fake_agent(tmp_path: Path) -> Path:
    """Write a bash fake-agent script that mirrors the helpers in test-wiggum-isolated-loop.sh.

    The script reads the prompt from stdin, logs invocation context into
    ``.claude/`` (which is excluded from workspace hashing), and behaves
    differently depending on (a) the leading argument it was invoked with
    (``good``/``poor``/``refusal``/``sentinel``/``noop``) and (b) markers
    inside the prompt (``MAKE_PASS``/``PROMISE``/``CHANGE_ONCE``).
    """

    script = tmp_path / "fake-agent.sh"
    script.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
NAME="${1:-worker}"
PROMPT="$(cat)"
mkdir -p .claude
printf '%s\\n' "$NAME" >> .claude/fake-agent-names.log
printf '%s\\n---END---\\n' "$PROMPT" >> .claude/fake-agent-prompts.log
case "$NAME" in
  good)
    echo good > result.txt
    echo "METRIC score=10"
    exit 0
    ;;
  poor)
    echo poor > result.txt
    echo "METRIC score=1"
    exit 0
    ;;
  refusal)
    # Says it cannot help but writes no files (stagnation should beat refusal regex).
    echo "I cannot do that"
    exit 0
    ;;
  sentinel)
    # Tries to create a sentinel file the test will look for in the main tree.
    echo sentinel-content > sentinel.txt
    exit 0
    ;;
esac
case "$PROMPT" in
  *MAKE_PASS*) touch pass.flag; echo "made pass" ;;
  *PROMISE*) echo "<promise>DONE</promise>" ;;
  *CHANGE_ONCE*) if [[ ! -f changed.txt ]]; then echo changed > changed.txt; fi; echo "changed maybe" ;;
  *) echo "noop" ;;
esac
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _force_remove_readonly(path: Path) -> None:
    """Best-effort recursive remove that restores write perms before unlink."""

    if not path.exists():
        return
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            try:
                os.chmod(os.path.join(dirpath, name), stat.S_IRWXU)
            except OSError:
                pass
    try:
        os.chmod(path, stat.S_IRWXU)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def readonly_home(tmp_path: Path):
    """Create a read-only HOME directory; restore perms in teardown.

    Skips on platforms where chmod 555 doesn't actually prevent writes (root,
    or filesystems that ignore the bits).
    """

    home = tmp_path / "ro_home"
    home.mkdir()
    os.chmod(home, 0o555)
    # Sanity check: confirm the OS actually enforces it.
    try:
        probe = home / "_probe"
        try:
            probe.write_text("x")
        except (PermissionError, OSError):
            pass
        else:
            probe.unlink(missing_ok=True)
            os.chmod(home, 0o755)
            pytest.skip("filesystem does not enforce chmod 555 on directories")
    finally:
        pass
    try:
        yield home
    finally:
        try:
            os.chmod(home, 0o755)
        except OSError:
            pass
        _force_remove_readonly(home)


@pytest.fixture
def isolated_run():
    """Return a helper that runs the real wiggum-isolated-loop.sh as a subprocess.

    The helper guarantees ``HOME`` is set, the subprocess never inherits the
    test runner's git global config, and the project's ``scripts/`` wrapper is
    invoked exactly as a user would invoke it from a shell.
    """

    def _run(
        project_dir: Path,
        prompt: str,
        *flags: str,
        env_overrides: dict | None = None,
        timeout: int = 60,
    ) -> subprocess.CompletedProcess:
        wrapper = SCRIPTS_DIR / "wiggum-isolated-loop.sh"
        env = os.environ.copy()
        env.setdefault("HOME", str(project_dir / "_home"))
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        env["GIT_AUTHOR_NAME"] = "wiggum"
        env["GIT_AUTHOR_EMAIL"] = "wiggum@example.invalid"
        env["GIT_COMMITTER_NAME"] = "wiggum"
        env["GIT_COMMITTER_EMAIL"] = "wiggum@example.invalid"
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run(
            [str(wrapper), prompt, *flags],
            cwd=str(project_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )

    return _run
