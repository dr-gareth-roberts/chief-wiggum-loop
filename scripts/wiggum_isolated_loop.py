#!/usr/bin/env python3
"""Run Wiggum Loop as true isolated agent invocations.

This runner is the long-run alternative to Stop-hook Ralph loops. It launches
fresh agent subprocesses, carries only bounded file/ledger memory forward, and
optionally isolates candidate patches in temporary git worktrees before deciding
what to keep.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

STATE_REL = Path(".claude/wiggum-isolated.local.json")
PROMPT_REL = Path(".claude/wiggum-isolated-prompt.local.md")
LOG_REL = Path(".claude/wiggum-isolated.log.jsonl")
SUMMARY_REL = Path(".claude/wiggum-isolated-summary.local.md")
DASHBOARD_MD_REL = Path(".claude/wiggum-dashboard.md")
DASHBOARD_HTML_REL = Path(".claude/wiggum-dashboard.html")
CHECKPOINT_REL = Path(".claude/wiggum-checkpoint.local.md")
ARCHIVE_DIR_REL = Path(".claude/wiggum-archive")
PROJECT_LESSONS_REL = Path(".claude/wiggum-lessons.jsonl")
GLOBAL_LESSONS = Path.home() / ".wiggum" / "lessons.jsonl"

DEFAULT_VARIANTS = [
    "Smallest verifiable improvement: change one thing and run the check.",
    "Bug-hunt pass: inspect current failures/diff, then fix the highest-risk issue.",
    "Completion pass: close one explicit acceptance criterion end-to-end.",
    "Regression pass: add or tighten a check before changing implementation.",
    "Stall-breaker: if no user files changed last iteration, modify the most relevant file now.",
]

PRESETS: dict[str, dict[str, Any]] = {
    "none": {},
    "coding": {"mode": "variants", "agent_switch_every": 4, "critic_every": 4},
    "review-heavy": {"mode": "variants", "agent_switch_every": 3, "critic_every": 3, "review_required": True},
    "cheap": {"mode": "reflective", "agent_switch_every": 6, "summary_max_chars": 4000},
    "explore": {"mode": "variants", "agent_switch_every": 2, "critic_every": 2, "candidates": 2},
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compact_text(text: str, max_chars: int) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half].rstrip() + "\n\n... [middle trimmed] ...\n\n" + text[-half:].lstrip()


def extract_tagged(body: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", body, flags=re.DOTALL)
    return re.sub(r"\s+", " ", match.group(1).strip()) if match else None


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


def append_log(cwd: Path, entry: dict[str, Any]) -> None:
    append_jsonl(cwd / LOG_REL, {"timestamp": utc_now(), **entry})


_GLOBAL_LESSONS_WARNED = False


def append_lesson(cwd: Path, lesson: dict[str, Any], global_enabled: bool = True) -> None:
    global _GLOBAL_LESSONS_WARNED
    entry = {"timestamp": utc_now(), "cwd": str(cwd), **lesson}
    append_jsonl(cwd / PROJECT_LESSONS_REL, entry)
    if global_enabled:
        exc = append_jsonl(GLOBAL_LESSONS, entry)
        if exc is not None and not _GLOBAL_LESSONS_WARNED:
            _GLOBAL_LESSONS_WARNED = True
            append_log(cwd, {"event": "global_lessons_disabled", "reason": str(exc)})


def archive_state(cwd: Path, state: dict[str, Any], reason: str) -> Path | None:
    state_path = cwd / STATE_REL
    if not state_path.exists():
        return None
    archive_dir = cwd / ARCHIVE_DIR_REL
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().replace(":", "").replace("-", "")
    archive_path = archive_dir / f"wiggum-isolated.{reason}.{stamp}.json"
    atomic_write_json(state_path, {**state, "active": False, "stop_reason": reason, "stopped_at": utc_now()})
    shutil.move(str(state_path), str(archive_path))
    return archive_path


def run_shell(command: str, cwd: Path, timeout: int, stdin_text: str | None = None) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            input=stdin_text,
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(1, timeout),
            check=False,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return {"exit_code": result.returncode, "output": output, "timeout": False, "duration_seconds": time.monotonic() - started}
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        return {"exit_code": 124, "output": output, "timeout": True, "duration_seconds": time.monotonic() - started}


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


def has_head(cwd: Path) -> bool:
    return run_git(cwd, ["rev-parse", "--verify", "HEAD"]).get("exit_code") == 0


def wiggum_pathspec() -> list[str]:
    # Treat .claude as loop/control metadata, not user progress. This prevents
    # prompt logs, summaries, dashboards, and hook state from defeating stuck detection.
    return ["--", ".", ":(exclude).claude", ":(exclude).claude/**"]


def workspace_hash(cwd: Path) -> str:
    digest = hashlib.sha256()
    if is_git_repo(cwd):
        pathspec = wiggum_pathspec()
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


def git_status_lines(cwd: Path, limit: int = 80) -> list[str]:
    if not is_git_repo(cwd):
        return []
    raw = git_bytes(cwd, ["status", "--porcelain=v1", *wiggum_pathspec()]).decode("utf-8", errors="replace")
    return [line for line in raw.splitlines() if line.strip()][:limit]


def git_diff_stat(cwd: Path) -> str:
    if not is_git_repo(cwd):
        return ""
    raw = git_bytes(cwd, ["diff", "--stat", *wiggum_pathspec()], timeout=8)
    staged = git_bytes(cwd, ["diff", "--cached", "--stat", *wiggum_pathspec()], timeout=8)
    return (raw + staged).decode("utf-8", errors="replace").strip()


def mark_untracked_for_diff(cwd: Path, allow_in_main: bool = False) -> None:
    # Refuse to mutate the user's main tree index unless the caller is
    # explicitly working inside a candidate sandbox. Touching the main
    # index from a no-op verification pass is a real-world hazard.
    if not is_git_repo(cwd) or not allow_in_main:
        return
    raw = git_bytes(cwd, ["ls-files", "--others", "--exclude-standard", "-z"])
    paths = [p for p in raw.split(b"\0") if p and not p.startswith(b".claude/wiggum-")]
    if paths:
        try:
            subprocess.run(["git", "add", "-N", "--", *[p.decode("utf-8", errors="replace") for p in paths]], cwd=str(cwd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        except Exception:
            pass


def make_patch(cwd: Path, allow_in_main: bool = False) -> str:
    if not is_git_repo(cwd):
        return ""
    mark_untracked_for_diff(cwd, allow_in_main=allow_in_main)
    return git_bytes(cwd, ["diff", "--binary", *wiggum_pathspec()], timeout=15).decode("utf-8", errors="replace")


def apply_patch(cwd: Path, patch: str) -> bool:
    if not patch.strip():
        return True
    check = run_shell("git apply --check --binary -", cwd, 20, patch)
    if check["exit_code"] != 0:
        return False
    result = run_shell("git apply --binary -", cwd, 30, patch)
    return result["exit_code"] == 0


def copy_current_tree(src: Path, dst: Path) -> None:
    ignore = shutil.ignore_patterns(".git", "node_modules", "__pycache__", ".claude/wiggum-*")
    shutil.copytree(src, dst, ignore=ignore)


def create_candidate_workspace(cwd: Path, sandbox: str, iteration: int, candidate: int) -> tuple[Path, str, Path | None]:
    tmp_root = Path(tempfile.mkdtemp(prefix=f"wiggum-i{iteration}-c{candidate}-"))
    if sandbox == "worktree" and is_git_repo(cwd) and has_head(cwd):
        worktree = tmp_root / "worktree"
        result = run_git(cwd, ["worktree", "add", "--detach", str(worktree), "HEAD"], timeout=30)
        if result.get("exit_code") == 0:
            base_patch = make_patch(cwd)
            if base_patch.strip():
                apply_patch(worktree, base_patch)
            # Turn the caller's current tracked/untracked baseline into the
            # candidate's temporary HEAD so the candidate patch contains only
            # this worker's delta, not pre-existing local files like logs.
            run_shell("git add -A && git -c user.email=wiggum@example.invalid -c user.name=Wiggum commit -q -m wiggum-candidate-baseline --no-verify || true", worktree, 20)
            return worktree, "worktree", tmp_root
    # Fallback copy sandbox works for repos without HEAD and non-git projects.
    copy_path = tmp_root / "copy"
    copy_current_tree(cwd, copy_path)
    return copy_path, "copy", tmp_root


def cleanup_candidate_workspace(cwd: Path, candidate_cwd: Path, kind: str, tmp_root: Path | None, keep: bool) -> None:
    if keep:
        return
    if kind == "worktree":
        run_git(cwd, ["worktree", "remove", "--force", str(candidate_cwd)], timeout=20)
    if tmp_root and tmp_root.exists():
        shutil.rmtree(tmp_root, ignore_errors=True)


def parse_metrics(text: str) -> dict[str, float]:
    metrics: dict[str, float] = {}
    patterns = [
        r"^\s*METRIC\s+([A-Za-z0-9_.:-]+)\s*=\s*(-?\d+(?:\.\d+)?)\s*$",
        r"^\s*([A-Za-z0-9_.:-]+)\s*=\s*(-?\d+(?:\.\d+)?)\s*$",
    ]
    for line in text.splitlines():
        for pattern in patterns:
            m = re.match(pattern, line)
            if m:
                try:
                    metrics[m.group(1)] = float(m.group(2))
                except ValueError:
                    pass
                break
    return metrics


def metric_improved(new: float | None, best: float | None, direction: str) -> bool:
    if new is None:
        return False
    if best is None:
        return True
    return new > best if direction == "higher" else new < best


def promise_was_met(output: str, expected: str | None) -> bool:
    if not expected:
        return False
    actual = extract_tagged(output, "promise")
    return actual is not None and actual == expected


def read_prompt(args: argparse.Namespace) -> str:
    parts: list[str] = []
    if args.prompt_file:
        parts.append(Path(args.prompt_file).read_text(encoding="utf-8"))
    if args.prompt:
        parts.append(" ".join(args.prompt))
    return "\n\n".join(p.strip() for p in parts if p.strip()).strip()


def read_summary(cwd: Path, max_chars: int) -> str:
    path = cwd / SUMMARY_REL
    if not path.exists():
        return ""
    return compact_text(path.read_text(encoding="utf-8", errors="replace"), max_chars)


def deterministic_summary_update(previous: str, round_info: dict[str, Any], max_chars: int) -> str:
    output_tail = compact_text(str(round_info.get("output_tail") or ""), 900)
    verifier_tail = compact_text(str(round_info.get("verifier_output_tail") or ""), 700)
    status_lines = round_info.get("status_lines") or []
    changed = "; ".join(status_lines[:20]) if status_lines else "no user-file changes detected"
    verifier = round_info.get("verifier_exit_code")
    verifier_text = "not configured" if verifier is None else str(verifier)
    no_progress = " yes" if round_info.get("stagnant_iterations", 0) else " no"
    if not previous.strip():
        previous = "# Wiggum isolated rolling summary\n\nThis file is generated so fresh isolated iterations can avoid repeating prior attempts.\n"
    entry = [
        "",
        f"## Iteration {round_info.get('iteration')} — {round_info.get('timestamp')}",
        f"- agent_exit: {round_info.get('agent_exit_code')} timeout: {round_info.get('agent_timeout')}",
        f"- candidate: {round_info.get('accepted_candidate', 'n/a')} / {round_info.get('candidate_count', 1)}",
        f"- verifier_exit: {verifier_text}",
        f"- metric: {round_info.get('metric_name') or 'n/a'}={round_info.get('metric_value') if round_info.get('metric_value') is not None else 'n/a'}",
        f"- stuck_reason: {round_info.get('stuck_reason') or 'none'}",
        f"- no_progress:{no_progress}; stagnant_count: {round_info.get('stagnant_iterations')}",
        f"- current changed/untracked files: {changed}",
    ]
    if output_tail:
        entry.extend(["- agent output tail:", "```", output_tail, "```"])
    if verifier_tail:
        entry.extend(["- verifier output tail:", "```", verifier_tail, "```"])
    if round_info.get("stagnant_iterations", 0):
        entry.append("- lesson: this round did not change user-visible workspace state; do not repeat the same no-op next round.")
    if verifier not in (None, 0):
        entry.append("- lesson: verifier is still failing; next round should target the concrete verifier output above.")
    if round_info.get("stuck_reason"):
        entry.append(f"- lesson: classified failure mode is `{round_info['stuck_reason']}`; choose a different tactic.")
    return compact_text(previous.rstrip() + "\n" + "\n".join(entry) + "\n", max_chars)


def update_memory_summary(cwd: Path, args: argparse.Namespace, round_info: dict[str, Any]) -> str:
    if args.memory_mode == "none":
        return ""
    previous = read_summary(cwd, args.summary_max_chars)
    payload = json.dumps(
        {
            "instruction": "Update the rolling Wiggum summary. Preserve attempts, verifier failures, dead ends, metrics, critic findings, and next hints. Return markdown only.",
            "previous_summary": previous,
            "latest_round": round_info,
            "max_chars": args.summary_max_chars,
        },
        indent=2,
        sort_keys=True,
    )
    if args.summary_command:
        result = run_shell(args.summary_command, cwd, args.summary_timeout, payload)
        output = str(result.get("output") or "").strip()
        summary = compact_text(output, args.summary_max_chars) if result.get("exit_code") == 0 and output else deterministic_summary_update(previous, {**round_info, "summary_command_error_tail": output[-1000:]}, args.summary_max_chars)
    else:
        summary = deterministic_summary_update(previous, round_info, args.summary_max_chars)
    path = cwd / SUMMARY_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(summary.rstrip() + "\n", encoding="utf-8")
    return summary


def variant_for(iteration: int, stagnant: int) -> str:
    if stagnant > 0:
        return DEFAULT_VARIANTS[-1]
    return DEFAULT_VARIANTS[(iteration - 1) % len(DEFAULT_VARIANTS)]


def select_agent_command(args: argparse.Namespace, iteration: int, candidate: int = 1) -> tuple[str, int]:
    commands = list(args.agent_command)
    # Candidate fan-out intentionally staggers across model commands so best-of-N can compare styles.
    index = (((iteration - 1) // args.agent_switch_every) + (candidate - 1)) % len(commands)
    return commands[index], index


def recent_log_entries(cwd: Path, limit: int = 8) -> list[dict[str, Any]]:
    path = cwd / LOG_REL
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def append_section_to_summary(cwd: Path, title: str, body: str, max_chars: int) -> str:
    previous = read_summary(cwd, max_chars) or "# Wiggum isolated rolling summary\n"
    summary = compact_text(previous.rstrip() + f"\n\n## {title} — {utc_now()}\n\n{body.strip()}\n", max_chars)
    (cwd / SUMMARY_REL).write_text(summary.rstrip() + "\n", encoding="utf-8")
    return summary


def build_meta_review_prompt(kind: str, core_prompt: str, args: argparse.Namespace, state: dict[str, Any], reason: str = "") -> str:
    cwd = Path.cwd()
    payload = {
        "role": kind,
        "instruction": (
            "Return markdown only. Do not edit files. Identify repeated failed attempts, dead ends, missing checks, and the highest-leverage next move."
            if kind == "critic"
            else f"Return markdown plus <review>{args.review_approval_token}</review> only if the loop should stop. If anything important is missing, explain the blocker and do not approve. Do not edit files."
        ),
        "stop_reason_under_review": reason,
        "core_prompt": core_prompt,
        "rolling_summary": read_summary(cwd, args.summary_max_chars),
        "recent_log_entries": recent_log_entries(cwd),
        "git_status": git_status_lines(cwd),
        "git_diff_stat": git_diff_stat(cwd),
        "success_command": args.success_command,
        "metric": {"name": args.metric_name, "direction": args.metric_direction, "best": state.get("best_metric")},
        "state": state,
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def run_agent(command: str, prompt: str, cwd: Path, timeout: int, iteration: int) -> dict[str, Any]:
    started = time.monotonic()
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as f:
        f.write(prompt)
        prompt_file = f.name
    try:
        rendered = command.replace("{prompt_file}", shlex.quote(prompt_file)).replace("{iteration}", str(iteration))
        result = run_shell(rendered, cwd, timeout, None if "{prompt_file}" in command else prompt)
    finally:
        try:
            os.unlink(prompt_file)
        except OSError:
            pass
    output = str(result.get("output") or "")
    return {
        "exit_code": result.get("exit_code"),
        "timeout": result.get("timeout", False),
        "duration_seconds": time.monotonic() - started,
        "output_tail": output[-4000:],
        "estimated_tokens": max(1, (len(prompt) + len(output)) // 4),
    }


def classify_stuck_reason(candidate: dict[str, Any], stagnant: int, previous_failure_hash: str | None) -> str:
    output = str(candidate.get("output_tail") or "")
    verifier = str(candidate.get("verifier_output_tail") or "")
    if candidate.get("agent_timeout") or candidate.get("timeout"):
        return "agent_timeout"
    if re.search(r"\b(can't|cannot|unable to|give up|not possible|as an ai)\b", output, re.I):
        return "model_refusal_or_give_up"
    if stagnant > 0:
        return "no_workspace_progress"
    if candidate.get("verifier_exit_code") not in (None, 0) and not verifier.strip():
        return "verifier_failed_without_output"
    failure_hash = hashlib.sha256(verifier.encode()).hexdigest() if verifier else ""
    if previous_failure_hash and failure_hash and previous_failure_hash == failure_hash:
        return "same_verifier_failure"
    if candidate.get("metric_required") and candidate.get("metric_value") is None:
        return "metric_missing"
    if candidate.get("accepted") is False:
        return "patch_rejected_by_policy"
    return ""


def mutate_prompt_hint(args: argparse.Namespace, state: dict[str, Any], stuck_reason: str) -> str:
    if args.prompt_mutation == "none" or not stuck_reason:
        return ""
    payload = {
        "instruction": "Rewrite the next-iteration tactical hint only. Do not rewrite the core task. Avoid previous failed approach.",
        "stuck_reason": stuck_reason,
        "rolling_summary": read_summary(Path.cwd(), args.summary_max_chars),
        "recent_log_entries": recent_log_entries(Path.cwd()),
    }
    if args.prompt_mutation == "command" and args.mutation_command:
        result = run_shell(args.mutation_command, Path.cwd(), args.summary_timeout, json.dumps(payload, indent=2))
        output = str(result.get("output") or "").strip()
        if result.get("exit_code") == 0 and output:
            return compact_text(output, 1200)
    return f"Prompt mutation hint: Previous failure mode was `{stuck_reason}`. Do not repeat the same approach; inspect a different source of evidence, change a smaller patch, and target the verifier/metric directly."


def build_iteration_prompt(core_prompt: str, args: argparse.Namespace, iteration: int, stagnant: int, last_agent: dict[str, Any] | None, memory_summary: str = "", mutation_hint: str = "") -> str:
    if args.mode == "exact" and not mutation_hint:
        return core_prompt + "\n"
    lines = [
        core_prompt.rstrip(),
        "",
        "---",
        "Wiggum isolated controller:",
        "- This is a fresh isolated agent invocation. Do not rely on prior chat context; inspect files instead.",
        f"- Iteration: {iteration}" + (f" of {args.max_iterations}" if args.max_iterations else " (unbounded)"),
        f"- Progress ledger: {LOG_REL}",
        f"- No-progress count before this run: {stagnant}",
    ]
    if args.success_command:
        lines.append(f"- Success command: `{args.success_command}`")
    if args.metric_name:
        lines.append(f"- Metric target: `{args.metric_name}` direction `{args.metric_direction}`; emit or parse `METRIC {args.metric_name}=...`.")
    if args.completion_promise:
        lines.append(f"- Completion promise: output <promise>{args.completion_promise}</promise> only when true.")
    lines.append(f"- This iteration's angle: {variant_for(iteration, stagnant) if args.mode == 'variants' else 'inspect current repo state, make one concrete improvement, verify it.'}")
    if mutation_hint:
        lines.append(f"- {mutation_hint}")
    if memory_summary.strip():
        lines.extend(["- Prior rounds summary (avoid repeating failed/no-op attempts):", "```markdown", compact_text(memory_summary, args.summary_max_chars), "```"])
    if last_agent and last_agent.get("exit_code") not in (None, 0):
        tail = str(last_agent.get("output_tail") or "").strip()[-1800:]
        if tail:
            lines.extend(["- Previous agent output tail:", "```", tail, "```"])
    lines.extend([
        "- Keep changes grounded in the repo. Prefer tests/checks and durable files over chat-only plans.",
        "- If blocked, write the blocker and smallest next unblocker to a file in the repo.",
        "---",
        "",
    ])
    return "\n".join(lines)


def run_candidate(cwd: Path, core_prompt: str, args: argparse.Namespace, state: dict[str, Any], iteration: int, candidate_index: int, last_agent: dict[str, Any] | None, mutation_hint: str) -> dict[str, Any]:
    needs_sandbox = args.sandbox != "none" or args.candidates > 1 or args.acceptance in {"verifier", "metric", "progress"}
    sandbox = args.sandbox if needs_sandbox else "none"
    candidate_cwd = cwd
    sandbox_kind = "none"
    sandbox_root: Path | None = None
    if sandbox != "none":
        candidate_cwd, sandbox_kind, sandbox_root = create_candidate_workspace(cwd, sandbox, iteration, candidate_index)

    memory_summary = "" if args.mode == "exact" else read_summary(cwd, args.summary_max_chars)
    prompt = build_iteration_prompt(core_prompt, args, iteration, int(state.get("stagnant_iterations") or 0), last_agent, memory_summary, mutation_hint)
    agent_command, agent_command_index = select_agent_command(args, iteration, candidate_index)
    agent = run_agent(agent_command, prompt, candidate_cwd, args.agent_timeout, iteration)
    postcheck = run_shell(args.success_command, candidate_cwd, args.success_timeout) if args.success_command else None
    combined_output = (agent.get("output_tail") or "") + "\n" + (str(postcheck.get("output") or "") if postcheck else "")
    metrics = parse_metrics(combined_output)
    metric_value = metrics.get(args.metric_name) if args.metric_name else None
    # Only mark untracked files inside a real candidate sandbox so the
    # user's main-tree index is never mutated as a side-effect of patch
    # capture. When sandbox=none, candidate_cwd == cwd (main tree).
    in_sandbox = candidate_cwd != cwd
    patch = make_patch(candidate_cwd, allow_in_main=in_sandbox) if is_git_repo(candidate_cwd) else ""
    result = {
        **agent,
        "candidate": candidate_index,
        "candidate_cwd": str(candidate_cwd),
        "sandbox_kind": sandbox_kind,
        "sandbox_root": str(sandbox_root) if sandbox_root else "",
        "agent_command": agent_command,
        "agent_command_index": agent_command_index,
        "verifier_exit_code": postcheck.get("exit_code") if postcheck else None,
        "verifier_timeout": postcheck.get("timeout") if postcheck else False,
        "verifier_output_tail": str(postcheck.get("output") or "")[-3000:] if postcheck else "",
        "metrics": metrics,
        "metric_name": args.metric_name,
        "metric_value": metric_value,
        "metric_required": bool(args.metric_name and args.acceptance == "metric"),
        "patch": patch,
        "patch_chars": len(patch),
        "status_lines": git_status_lines(candidate_cwd),
    }
    return result


def candidate_rank(candidate: dict[str, Any], args: argparse.Namespace, best_metric: float | None) -> tuple[float, float, float]:
    metric = candidate.get("metric_value")
    verifier_bonus = 1.0 if candidate.get("verifier_exit_code") == 0 else 0.0
    progress_bonus = 1.0 if candidate.get("patch_chars", 0) > 0 else 0.0
    if args.metric_name and metric is not None:
        metric_score = float(metric) if args.metric_direction == "higher" else -float(metric)
    else:
        metric_score = 0.0
    # Prefer verifier success, then metric, then progress, then smaller patch for tie-breaking.
    return (verifier_bonus, metric_score, progress_bonus - min(candidate.get("patch_chars", 0), 100000) / 1_000_000_000)


def candidate_accepted(candidate: dict[str, Any], args: argparse.Namespace, state: dict[str, Any], baseline_hash: str) -> bool:
    policy = args.acceptance
    if policy == "auto":
        policy = "metric" if args.metric_name else "verifier" if args.success_command and (args.sandbox != "none" or args.candidates > 1) else "always"
    if policy == "always":
        return True
    if policy == "verifier":
        return candidate.get("verifier_exit_code") == 0
    if policy == "metric":
        return metric_improved(candidate.get("metric_value"), state.get("best_metric"), args.metric_direction)
    if policy == "progress":
        return candidate.get("patch_chars", 0) > 0
    return True


def apply_candidate_if_needed(cwd: Path, candidate: dict[str, Any], args: argparse.Namespace) -> bool:
    if candidate.get("sandbox_kind") == "none":
        return True
    patch = str(candidate.get("patch") or "")
    return apply_patch(cwd, patch)


def cleanup_candidates(cwd: Path, candidates: list[dict[str, Any]], keep_roots: set[str] | None = None) -> None:
    keep_roots = keep_roots or set()
    for cand in candidates:
        root = cand.get("sandbox_root")
        candidate_cwd = cand.get("candidate_cwd")
        if not root or root in keep_roots or not candidate_cwd:
            continue
        cleanup_candidate_workspace(cwd, Path(candidate_cwd), str(cand.get("sandbox_kind") or "copy"), Path(root), keep=False)


def run_critic_if_due(cwd: Path, core_prompt: str, args: argparse.Namespace, state: dict[str, Any], iteration: int) -> dict[str, Any] | None:
    if not args.critic_command or not args.critic_every or iteration % args.critic_every != 0:
        return None
    prompt = build_meta_review_prompt("critic", core_prompt, args, state)
    critic = run_agent(args.critic_command, prompt, cwd, args.critic_timeout, iteration)
    body = str(critic.get("output_tail") or "").strip()
    if body:
        append_section_to_summary(cwd, f"Critic review after iteration {iteration}", body, args.summary_max_chars)
    append_log(cwd, {"event": "critic", "iteration": iteration, "critic_exit_code": critic.get("exit_code"), "critic_timeout": critic.get("timeout", False), "output_tail": critic.get("output_tail", "")})
    return critic


def final_review_allows_exit(cwd: Path, core_prompt: str, args: argparse.Namespace, state: dict[str, Any], reason: str) -> tuple[bool, dict[str, Any] | None]:
    if not args.review_command:
        return True, None
    prompt = build_meta_review_prompt("final_reviewer", core_prompt, args, state, reason)
    review = run_agent(args.review_command, prompt, cwd, args.review_timeout, int(state.get("iteration") or 0))
    body = str(review.get("output_tail") or "").strip()
    if body:
        append_section_to_summary(cwd, f"Final review for {reason}", body, args.summary_max_chars)
    approved = extract_tagged(body, "review") == args.review_approval_token
    append_log(cwd, {"event": "final_review", "reason": reason, "iteration": state.get("iteration"), "approved": approved, "review_exit_code": review.get("exit_code"), "review_timeout": review.get("timeout", False), "output_tail": body})
    return approved, review


def render_dashboard(cwd: Path, state: dict[str, Any]) -> None:
    entries = recent_log_entries(cwd, limit=500)
    rows = []
    for e in entries:
        if e.get("event") not in {"iteration", "critic", "final_review", "stop", "pause", "human_checkpoint"}:
            continue
        rows.append(
            f"| {e.get('timestamp','')} | {e.get('event','')} | {e.get('iteration','')} | {e.get('agent_exit_code', e.get('critic_exit_code', e.get('review_exit_code','')))} | {e.get('verifier_exit_code','')} | {e.get('metric_value','')} | {e.get('stuck_reason','')} |"
        )
    md = [
        "# Wiggum Dashboard",
        "",
        f"Updated: {utc_now()}",
        f"State: iteration {state.get('iteration')} / {state.get('max_iterations')}",
        f"Best metric: {state.get('best_metric')}",
        f"Stagnant iterations: {state.get('stagnant_iterations')}",
        "",
        "| time | event | iter | agent/critic/review exit | verifier | metric | stuck reason |",
        "|---|---:|---:|---:|---:|---:|---|",
        *rows[-100:],
        "",
        f"Summary: `{SUMMARY_REL}`",
        f"Log: `{LOG_REL}`",
        f"Lessons: `{PROJECT_LESSONS_REL}` and `{GLOBAL_LESSONS}`",
    ]
    dashboard = "\n".join(md) + "\n"
    (cwd / DASHBOARD_MD_REL).write_text(dashboard, encoding="utf-8")
    html_doc = "<html><body><pre>" + html.escape(dashboard) + "</pre></body></html>\n"
    (cwd / DASHBOARD_HTML_REL).write_text(html_doc, encoding="utf-8")


def write_human_checkpoint(cwd: Path, state: dict[str, Any], reason: str) -> None:
    text = f"# Wiggum human checkpoint\n\nTime: {utc_now()}\n\nReason: {reason}\n\nIteration: {state.get('iteration')}\n\nSummary:\n\n{read_summary(cwd, 5000)}\n"
    (cwd / CHECKPOINT_REL).write_text(text, encoding="utf-8")
    append_log(cwd, {"event": "human_checkpoint", "reason": reason, "iteration": state.get("iteration"), "checkpoint": str(CHECKPOINT_REL)})


def maybe_pause_for_human(cwd: Path, args: argparse.Namespace, state: dict[str, Any], reason: str) -> bool:
    if args.human_checkpoint == "never":
        return False
    due = False
    if args.human_checkpoint == "always":
        due = True
    elif args.human_checkpoint == "on-stuck" and reason in {"stuck", "review_veto", "budget"}:
        due = True
    elif args.human_checkpoint == "on-critic" and state.get("last_critic"):
        due = True
    if args.human_checkpoint_every and state.get("iteration", 0) and state["iteration"] % args.human_checkpoint_every == 0:
        due = True
    if due:
        write_human_checkpoint(cwd, state, reason)
        return True
    return False


def budget_exceeded(args: argparse.Namespace, state: dict[str, Any], started: float) -> str:
    if args.max_runtime_seconds and time.monotonic() - started >= args.max_runtime_seconds:
        return "runtime_budget"
    if args.max_agent_runs and state.get("agent_runs", 0) >= args.max_agent_runs:
        return "agent_run_budget"
    if args.max_estimated_tokens and state.get("estimated_tokens", 0) >= args.max_estimated_tokens:
        return "estimated_token_budget"
    return ""


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run a true-isolated Wiggum loop using fresh agent subprocesses.")
    p.add_argument("prompt", nargs="*", help="Core prompt text")
    p.add_argument("--prompt-file", help="Read core prompt from a file")
    p.add_argument("--preset", choices=sorted(PRESETS), default="none", help="Behavior preset; explicit flags override it")
    p.add_argument("--agent-command", action="append", default=None, help="Fresh worker command. Repeat for multi-model rotation. Prompt is stdin unless {prompt_file} is used.")
    p.add_argument("--agent-switch-every", type=int, default=4, help="Switch worker command every N iterations")
    p.add_argument("--agent-timeout", type=int, default=600, help="Timeout per isolated agent run in seconds")
    p.add_argument("--max-iterations", type=int, default=12, help="Maximum iterations; 0 means unbounded")
    p.add_argument("--allow-infinite", action="store_true", help="Alias for --max-iterations 0")
    p.add_argument("--completion-promise", default=None, help="Stop when output contains exact <promise>TEXT</promise>")
    p.add_argument("--success-command", default="", help="Verifier command; stop when it exits 0 unless metric optimization continues")
    p.add_argument("--success-timeout", type=int, default=60)
    p.add_argument("--metric-name", default="", help="Metric name parsed from `METRIC name=value` lines")
    p.add_argument("--metric-direction", choices=["lower", "higher"], default="higher")
    p.add_argument("--acceptance", choices=["auto", "always", "verifier", "metric", "progress"], default="auto", help="Patch acceptance policy")
    p.add_argument("--sandbox", choices=["none", "worktree", "copy"], default="none", help="Run worker in main tree or isolated candidate workspace")
    p.add_argument("--candidates", type=int, default=1, help="Best-of-N candidates per iteration")
    p.add_argument("--candidate-concurrency", type=int, default=1, help="Parallel candidate workers")
    p.add_argument("--stuck-after", type=int, default=3, help="Pause after N no-progress iterations; 0 disables")
    p.add_argument("--prompt-mutation", choices=["none", "deterministic", "command"], default="deterministic")
    p.add_argument("--mutation-command", default="", help="Optional prompt-mutation command for tactical hint generation")
    p.add_argument("--mode", choices=["exact", "reflective", "variants"], default="reflective")
    p.add_argument("--memory-mode", choices=["summary", "none"], default="summary")
    p.add_argument("--summary-command", default="")
    p.add_argument("--summary-timeout", type=int, default=120)
    p.add_argument("--summary-max-chars", type=int, default=6000)
    p.add_argument("--critic-command", default="")
    p.add_argument("--critic-every", type=int, default=0)
    p.add_argument("--critic-timeout", type=int, default=300)
    p.add_argument("--review-command", default="")
    p.add_argument("--review-timeout", type=int, default=300)
    p.add_argument("--review-approval-token", default="APPROVED")
    p.add_argument("--max-runtime-seconds", type=int, default=0)
    p.add_argument("--max-agent-runs", type=int, default=0)
    p.add_argument("--max-estimated-tokens", type=int, default=0)
    p.add_argument("--human-checkpoint", choices=["never", "always", "on-stuck", "on-critic"], default="never")
    p.add_argument("--human-checkpoint-every", type=int, default=0)
    p.add_argument("--global-lessons", action="store_true", help="Opt in to appending lessons to ~/.wiggum/lessons.jsonl (default: project-only)")
    p.add_argument("--no-global-lessons", action="store_true", help="Deprecated alias kept for backwards compatibility; global lessons are now opt-in via --global-lessons")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    preset = PRESETS.get(args.preset, {})
    for key, value in preset.items():
        if key == "review_required":
            continue
        if getattr(args, key, None) in (None, "", 0, "reflective"):
            setattr(args, key, value)
    if args.allow_infinite:
        args.max_iterations = 0
    if not args.agent_command:
        env = os.environ.get("WIGGUM_AGENT_COMMAND")
        if env:
            args.agent_command = [env]
        elif args.preset == "explore":
            args.agent_command = [os.environ.get("WIGGUM_PRIMARY_AGENT", "claude --print"), os.environ.get("WIGGUM_SECONDARY_AGENT", "claude --print")]
        else:
            args.agent_command = ["claude --print"]
    if args.preset in {"coding", "review-heavy", "explore"} and not args.critic_command:
        args.critic_command = os.environ.get("WIGGUM_CRITIC_COMMAND", "")
    if args.preset == "review-heavy" and not args.review_command:
        args.review_command = os.environ.get("WIGGUM_REVIEW_COMMAND", "")
    numeric_ok = all(
        [
            args.max_iterations >= 0,
            args.agent_switch_every > 0,
            args.agent_timeout > 0,
            args.success_timeout > 0,
            args.summary_timeout > 0,
            args.summary_max_chars >= 1000,
            args.critic_every >= 0,
            args.critic_timeout > 0,
            args.review_timeout > 0,
            args.stuck_after >= 0,
            args.candidates > 0,
            args.candidate_concurrency > 0,
            args.max_runtime_seconds >= 0,
            args.max_agent_runs >= 0,
            args.max_estimated_tokens >= 0,
            args.human_checkpoint_every >= 0,
        ]
    )
    if not numeric_ok:
        p.error("numeric flags must be non-negative and timeouts/counts must be positive where applicable")
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    cwd = Path.cwd()
    # Global lessons are now opt-in: --global-lessons turns them on;
    # the legacy --no-global-lessons flag still suppresses them.
    global_enabled = bool(args.global_lessons) and not bool(args.no_global_lessons)
    core_prompt = read_prompt(args)
    if not core_prompt:
        print("No prompt provided. Pass prompt text or --prompt-file.", file=sys.stderr)
        return 2
    (cwd / PROMPT_REL).parent.mkdir(parents=True, exist_ok=True)
    (cwd / PROMPT_REL).write_text(core_prompt + "\n", encoding="utf-8")
    started = time.monotonic()
    state: dict[str, Any] = {
        "active": True,
        "started_at": utc_now(),
        "preset": args.preset,
        "agent_commands": args.agent_command,
        "agent_switch_every": args.agent_switch_every,
        "agent_timeout": args.agent_timeout,
        "max_iterations": args.max_iterations,
        "completion_promise": args.completion_promise,
        "success_command": args.success_command,
        "success_timeout": args.success_timeout,
        "metric_name": args.metric_name,
        "metric_direction": args.metric_direction,
        "best_metric": None,
        "acceptance": args.acceptance,
        "sandbox": args.sandbox,
        "candidates": args.candidates,
        "stuck_after": args.stuck_after,
        "mode": args.mode,
        "memory_mode": args.memory_mode,
        "summary_max_chars": args.summary_max_chars,
        "critic_command": args.critic_command,
        "critic_every": args.critic_every,
        "review_command": args.review_command,
        "prompt_path": str(PROMPT_REL),
        "summary_path": str(SUMMARY_REL),
        "last_workspace_hash": workspace_hash(cwd),
        "last_failure_hash": "",
        "stagnant_iterations": 0,
        "agent_runs": 0,
        "estimated_tokens": 0,
        "iteration": 0,
    }
    atomic_write_json(cwd / STATE_REL, state)
    if args.dry_run:
        print(build_iteration_prompt(core_prompt, args, 1, 0, None, read_summary(cwd, args.summary_max_chars)))
        return 0
    print(f"🔁 Wiggum isolated loop starting: mode={args.mode}, agents={args.agent_command!r}, candidates={args.candidates}", flush=True)
    last_agent: dict[str, Any] | None = None
    mutation_hint = ""
    iteration = 0

    while args.max_iterations == 0 or iteration < args.max_iterations:
        budget = budget_exceeded(args, state, started)
        if budget:
            if maybe_pause_for_human(cwd, args, state, "budget"):
                archive = archive_state(cwd, state, "human-checkpoint")
                print(f"⏸️ Human checkpoint written for budget; archived state at {archive}")
                return 0
            archive = archive_state(cwd, state, budget)
            append_log(cwd, {"event": "stop", "reason": budget, "iteration": iteration, "archive": str(archive)})
            print(f"🛑 Budget reached ({budget}); archived state at {archive}")
            return 0

        iteration += 1
        state["iteration"] = iteration
        atomic_write_json(cwd / STATE_REL, state)
        if args.success_command and not args.review_command and not args.metric_name:
            precheck = run_shell(args.success_command, cwd, args.success_timeout)
            if precheck["exit_code"] == 0:
                archive = archive_state(cwd, state, "pre-verifier")
                append_log(cwd, {"event": "stop", "reason": "pre_verifier", "iteration": iteration, "archive": str(archive)})
                print(f"✅ Success command already passes; archived state at {archive}")
                return 0

        baseline_hash = workspace_hash(cwd)
        candidate_indexes = list(range(1, args.candidates + 1))
        candidates: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.candidate_concurrency, args.candidates)) as ex:
            futures = [ex.submit(run_candidate, cwd, core_prompt, args, state, iteration, idx, last_agent, mutation_hint) for idx in candidate_indexes]
            for fut in concurrent.futures.as_completed(futures):
                candidates.append(fut.result())
        candidates.sort(key=lambda c: c.get("candidate", 0))
        best = max(candidates, key=lambda c: candidate_rank(c, args, state.get("best_metric")))
        accepted = candidate_accepted(best, args, state, baseline_hash)
        best["accepted"] = accepted
        applied = False
        if accepted:
            applied = apply_candidate_if_needed(cwd, best, args)
            best["applied"] = applied
            if not applied:
                accepted = False
                best["accepted"] = False
                best["apply_failed"] = True
        cleanup_candidates(cwd, candidates)

        current_hash = workspace_hash(cwd)
        stagnant = int(state.get("stagnant_iterations") or 0)
        stagnant = stagnant + 1 if current_hash == baseline_hash else 0
        verifier_tail = str(best.get("verifier_output_tail") or "")
        failure_hash = hashlib.sha256(verifier_tail.encode()).hexdigest() if verifier_tail else ""
        stuck_reason = classify_stuck_reason(best, stagnant, state.get("last_failure_hash") or "")
        metric_value = best.get("metric_value")
        if args.metric_name and metric_improved(metric_value, state.get("best_metric"), args.metric_direction):
            state["best_metric"] = metric_value
        state["last_failure_hash"] = failure_hash or state.get("last_failure_hash", "")
        state["agent_runs"] = int(state.get("agent_runs") or 0) + len(candidates)
        state["estimated_tokens"] = int(state.get("estimated_tokens") or 0) + sum(int(c.get("estimated_tokens") or 0) for c in candidates)
        state.update({"last_workspace_hash": current_hash, "stagnant_iterations": stagnant, "last_agent": best, "last_stuck_reason": stuck_reason, "updated_at": utc_now()})

        round_info = {
            "timestamp": utc_now(),
            "event": "iteration",
            "iteration": iteration,
            "candidate_count": len(candidates),
            "accepted_candidate": best.get("candidate"),
            "accepted": accepted,
            "applied": best.get("applied", best.get("sandbox_kind") == "none"),
            "agent_command_index": best.get("agent_command_index"),
            "agent_command": best.get("agent_command"),
            "agent_exit_code": best.get("exit_code"),
            "agent_timeout": best.get("timeout", False),
            "verifier_exit_code": best.get("verifier_exit_code"),
            "verifier_timeout": best.get("verifier_timeout", False),
            "stagnant_iterations": stagnant,
            "stuck_reason": stuck_reason,
            "status_lines": git_status_lines(cwd),
            "output_tail": best.get("output_tail", ""),
            "verifier_output_tail": best.get("verifier_output_tail", ""),
            "metric_name": args.metric_name,
            "metric_value": metric_value,
            "best_metric": state.get("best_metric"),
            "patch_chars": best.get("patch_chars", 0),
            "candidate_summaries": [
                {"candidate": c.get("candidate"), "agent_index": c.get("agent_command_index"), "verifier": c.get("verifier_exit_code"), "metric": c.get("metric_value"), "patch_chars": c.get("patch_chars"), "accepted": c is best and accepted}
                for c in candidates
            ],
        }
        updated_summary = update_memory_summary(cwd, args, round_info) if args.mode != "exact" else ""
        state["last_summary_chars"] = len(updated_summary)
        append_log(cwd, round_info)
        if stuck_reason:
            append_lesson(cwd, {"event": "iteration_lesson", "iteration": iteration, "stuck_reason": stuck_reason, "summary": compact_text(best.get("output_tail", ""), 500)}, global_enabled)
        critic = run_critic_if_due(cwd, core_prompt, args, state, iteration)
        if critic:
            state["last_critic"] = critic
        mutation_hint = mutate_prompt_hint(args, state, stuck_reason)
        atomic_write_json(cwd / STATE_REL, state)
        render_dashboard(cwd, state)
        print(f"↻ isolated iteration {iteration}: accepted={accepted} candidate={best.get('candidate')} exit={best.get('exit_code')} verifier={best.get('verifier_exit_code')} metric={metric_value} stagnant={stagnant} reason={stuck_reason or '-'}", flush=True)

        if promise_was_met(str(best.get("output_tail") or ""), args.completion_promise):
            approved, _review = final_review_allows_exit(cwd, core_prompt, args, state, "promise")
            if approved:
                archive = archive_state(cwd, state, "promise")
                append_log(cwd, {"event": "stop", "reason": "promise", "iteration": iteration, "archive": str(archive)})
                render_dashboard(cwd, state)
                print(f"✅ Completion promise detected; archived state at {archive}")
                return 0
            append_lesson(cwd, {"event": "review_veto", "reason": "promise", "iteration": iteration}, global_enabled)
            print("⚠️ Final reviewer did not approve promise exit; continuing.", flush=True)

        verifier_success = best.get("verifier_exit_code") == 0
        should_stop_on_verifier = verifier_success and not args.metric_name
        if should_stop_on_verifier:
            approved, _review = final_review_allows_exit(cwd, core_prompt, args, state, "verifier")
            if approved:
                archive = archive_state(cwd, state, "verifier")
                append_log(cwd, {"event": "stop", "reason": "verifier", "iteration": iteration, "archive": str(archive)})
                render_dashboard(cwd, state)
                print(f"✅ Success command passed after iteration {iteration}; archived state at {archive}")
                if args.review_command:
                    print("✅ Final review approved success exit.", flush=True)
                return 0
            append_lesson(cwd, {"event": "review_veto", "reason": "verifier", "iteration": iteration}, global_enabled)
            print("⚠️ Final reviewer did not approve verifier exit; continuing.", flush=True)

        if args.human_checkpoint_every and iteration % args.human_checkpoint_every == 0:
            if maybe_pause_for_human(cwd, args, state, "scheduled"):
                archive = archive_state(cwd, state, "human-checkpoint")
                print(f"⏸️ Human checkpoint written; archived state at {archive}")
                return 0
        if args.stuck_after and stagnant >= args.stuck_after:
            if maybe_pause_for_human(cwd, args, state, "stuck"):
                archive = archive_state(cwd, state, "human-checkpoint")
                print(f"⏸️ Human checkpoint written for stuck loop; archived state at {archive}")
                return 0
            archive = archive_state(cwd, state, "stuck")
            append_log(cwd, {"event": "pause", "reason": "stuck", "iteration": iteration, "archive": str(archive)})
            append_lesson(cwd, {"event": "stuck_pause", "iteration": iteration, "stuck_reason": stuck_reason}, global_enabled)
            render_dashboard(cwd, state)
            print(f"⏸️ Paused after {stagnant} no-progress isolated iteration(s); archived state at {archive}")
            return 0

    archive = archive_state(cwd, state, "max-iterations")
    append_log(cwd, {"event": "stop", "reason": "max_iterations", "iteration": iteration, "archive": str(archive)})
    render_dashboard(cwd, state)
    print(f"🛑 Max iterations ({args.max_iterations}) reached; archived state at {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
