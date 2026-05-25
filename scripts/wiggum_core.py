"""Shared helpers for the Wiggum Loop plugin.

This module is co-located with ``wiggum_isolated_loop.py`` in ``scripts/`` and
is imported by both that script and ``hooks/wiggum_stop_hook.py`` (which adds
``scripts/`` to ``sys.path`` via a shim near the top of the hook file).

The contents are intentionally stdlib-only so the plugin keeps zero external
dependencies and can be invoked from either entry point without setup.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

# Both entry points archive completed/aborted state under the same relative
# directory; this is a pure implementation detail of ``archive_state`` and is
# kept here (rather than in each entry point) so the shared helper has a
# single source of truth for the archive layout.
ARCHIVE_DIR_REL = Path(".claude/wiggum-archive")


# --- time --------------------------------------------------------------------


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --- text --------------------------------------------------------------------


def compact_text(text: str, max_chars: int) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half].rstrip() + "\n\n... [middle trimmed] ...\n\n" + text[-half:].lstrip()


# --- json IO -----------------------------------------------------------------


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, entry: dict[str, Any]) -> Exception | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
    except (PermissionError, OSError) as exc:
        return exc
    return None


# --- archival ----------------------------------------------------------------


def archive_state(
    cwd: Path,
    state: dict[str, Any],
    reason: str,
    *,
    prefix: str,
    state_path: Path,
) -> Path | None:
    """Move ``state_path`` into the archive dir, marking the run inactive.

    ``prefix`` distinguishes the two entry points' archives:
    ``wiggum-isolated`` for the isolated-loop runner and ``wiggum-loop`` for
    the stop-hook runner. The archive filename is
    ``{prefix}.{reason}.{stamp}.json``.
    """

    if not state_path.exists():
        return None
    archive_dir = cwd / ARCHIVE_DIR_REL
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().replace(":", "").replace("-", "")
    archive_path = archive_dir / f"{prefix}.{reason}.{stamp}.json"
    atomic_write_json(state_path, {**state, "active": False, "stop_reason": reason, "stopped_at": utc_now()})
    shutil.move(str(state_path), str(archive_path))
    return archive_path


# --- git plumbing ------------------------------------------------------------


def run_git(cwd: Path, args: list[str], timeout: int = 8) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    except Exception as exc:
        return {"exit_code": 1, "stdout": b"", "stderr": str(exc).encode()}


def git_bytes(cwd: Path, args: list[str], timeout: int = 8) -> bytes:
    return bytes(run_git(cwd, args, timeout).get("stdout") or b"")


def is_git_repo(cwd: Path) -> bool:
    return (cwd / ".git").exists() and run_git(cwd, ["rev-parse", "--is-inside-work-tree"]).get("exit_code") == 0


# --- pathspec variants -------------------------------------------------------


def wiggum_pathspec_isolated() -> list[str]:
    """Pathspec for the isolated loop runner.

    Treats the entire ``.claude`` directory as loop/control metadata; user
    progress detection ignores everything under it. Aggressive but safe for
    the isolated runner because it owns its own workspace state and uses
    candidate sandboxes for patch capture.
    """

    return ["--", ".", ":(exclude).claude", ":(exclude).claude/**"]


def wiggum_pathspec_stop_hook() -> list[str]:
    """Pathspec for the Stop-hook runner.

    Excludes only Wiggum's own state/ledger files inside ``.claude``, so user
    activity in ``.claude/other-tool/`` still counts as progress. The hook
    runs against the user's live tree on every Stop event and must not mask
    unrelated edits the user is making elsewhere under ``.claude``.
    """

    return ["--", ".", ":(exclude).claude/wiggum-*", ":(exclude).claude/wiggum-archive/*"]


# --- workspace hashing -------------------------------------------------------


def workspace_hash(cwd: Path, pathspec_fn: Callable[[], list[str]]) -> str:
    """Stable hash of the workspace's user-visible state.

    The caller injects which pathspec to use because the two entry points
    have different opinions about what counts as user progress (see the two
    ``wiggum_pathspec_*`` helpers above).
    """

    digest = hashlib.sha256()
    if is_git_repo(cwd):
        pathspec = pathspec_fn()
        for args in (
            ["status", "--porcelain=v1", "-z", *pathspec],
            ["diff", "--binary", *pathspec],
            ["diff", "--cached", "--binary", *pathspec],
        ):
            digest.update(b"\0".join(arg.encode() for arg in args) + b"\0")
            digest.update(git_bytes(cwd, args))
        return digest.hexdigest()

    ignored_dirs = {".git", ".claude", "node_modules", "__pycache__"}
    for path in sorted(p for p in cwd.rglob("*") if p.is_file() and not (set(p.relative_to(cwd).parts) & ignored_dirs)):
        digest.update(str(path.relative_to(cwd)).encode() + b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            pass
    return digest.hexdigest()
