#!/usr/bin/env python3
"""Claude Code Stop hook for Wiggum Loop.

This is a progress-aware variation on the Anthropic Ralph Loop plugin.  It
keeps the same core prompt, but can add iteration steering, run an optional
verifier command, pause on no filesystem progress, and maintain a JSONL ledger.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# Make the sibling ``scripts/`` directory importable so we can share helpers
# with ``wiggum_isolated_loop.py`` instead of duplicating them here.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from wiggum_core import (  # noqa: E402  (sys.path manipulation must come first)
    append_jsonl,
    archive_state as _archive_state,
    atomic_write_json,
    utc_now,
    wiggum_pathspec_stop_hook,
    workspace_hash as _workspace_hash,
)

STATE_REL = Path(".claude/wiggum-loop.local.json")
PROMPT_REL = Path(".claude/wiggum-prompt.local.md")
LOG_REL = Path(".claude/wiggum-loop.log.jsonl")

DEFAULT_VARIANTS = [
    "Make the smallest concrete improvement that moves the task toward done, then verify it.",
    "Audit your previous iteration first. Fix the highest-risk gap rather than adding breadth.",
    "If tests or checks failed, reproduce the failure, isolate the cause, and make one targeted fix.",
    "If you are only polishing, switch to measurable acceptance criteria and close one criterion fully.",
    "If no files changed last time, inspect the repo, choose a file to improve, and write the change now.",
]


def debug(message: str) -> None:
    if os.environ.get("WIGGUM_DEBUG"):
        print(f"[wiggum-debug] {message}", file=sys.stderr, flush=True)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def append_log(cwd: Path, entry: dict[str, Any]) -> None:
    append_jsonl(cwd / LOG_REL, {"timestamp": utc_now(), **entry})


def archive_state(cwd: Path, state_path: Path, state: dict[str, Any], reason: str) -> Path | None:
    # Bind the stop-hook's archive prefix for the shared helper. The signature
    # accepts ``state_path`` explicitly because different callers in this file
    # already had ``state_path`` in scope; passing it through preserves their
    # call sites unchanged.
    return _archive_state(cwd, state, reason, prefix="wiggum-loop", state_path=state_path)


def parse_hook_input() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}


def extract_text_blocks(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
    return texts


def last_assistant_text(transcript_path: str | None) -> str:
    if not transcript_path:
        return ""
    path = Path(transcript_path)
    if not path.exists():
        return ""

    texts: list[str] = []
    # Keep bounded for very long transcripts while still handling one-block-per-line logs.
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = obj.get("message") if isinstance(obj, dict) else None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        texts.extend(extract_text_blocks(message.get("content")))
    return texts[-1] if texts else ""


def normalize_promise(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def promise_was_met(last_output: str, expected: str | None) -> bool:
    if not expected:
        return False
    match = re.search(r"<promise>(.*?)</promise>", last_output, flags=re.DOTALL)
    return bool(match and normalize_promise(match.group(1)) == expected)


def run_command(command: str, cwd: Path, timeout_seconds: int) -> dict[str, Any]:
    if not command:
        return {"configured": False}
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(1, timeout_seconds),
        )
        output = (result.stdout + result.stderr)[-4000:]
        return {"configured": True, "exit_code": result.returncode, "output_tail": output}
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or "") + (exc.stderr or ""))[-4000:]
        return {"configured": True, "exit_code": 124, "output_tail": output, "timeout": True}


def select_variant(state: dict[str, Any], next_iteration: int, stagnant_iterations: int) -> str:
    custom = state.get("prompt_variants")
    variants = custom if isinstance(custom, list) and custom else DEFAULT_VARIANTS
    if stagnant_iterations > 0:
        return variants[-1]
    return variants[(max(next_iteration, 1) - 1) % len(variants)]


def read_prompt(cwd: Path, state: dict[str, Any]) -> str:
    prompt_path = Path(state.get("prompt_path") or PROMPT_REL)
    if not prompt_path.is_absolute():
        prompt_path = cwd / prompt_path
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")
    return str(state.get("prompt") or "")


def build_reason(
    prompt: str,
    state: dict[str, Any],
    next_iteration: int,
    stagnant_iterations: int,
    verifier: dict[str, Any],
) -> tuple[str, str]:
    max_iterations = int(state.get("max_iterations") or 0)
    completion = state.get("completion_promise")
    mode = str(state.get("mode") or "reflective")
    verifier_line = "not configured"
    if verifier.get("configured"):
        verifier_line = f"exit {verifier.get('exit_code')}"
        if verifier.get("timeout"):
            verifier_line += " (timeout)"
    system_message = (
        f"🔁 Wiggum loop iteration {next_iteration}"
        + (f"/{max_iterations}" if max_iterations > 0 else " (unbounded)")
        + f" | verifier: {verifier_line} | stagnant: {stagnant_iterations}"
    )

    if mode == "exact":
        return prompt, system_message

    control_lines = [
        "",
        "---",
        "Wiggum loop controller (variation on Ralph):",
        f"- Iteration: {next_iteration}" + (f" of {max_iterations}" if max_iterations > 0 else " (no max)"),
        f"- Last verifier: {verifier_line}",
        f"- Repeated workspace hash count: {stagnant_iterations}",
    ]
    if completion:
        control_lines.append(f"- Completion promise: output <promise>{completion}</promise> only when true.")
    if verifier.get("configured") and verifier.get("exit_code") != 0:
        tail = str(verifier.get("output_tail") or "").strip()
        if tail:
            control_lines.extend(["- Verifier output tail:", "```", tail[-1800:], "```"])
    if mode == "variants":
        control_lines.append(f"- This iteration's angle: {select_variant(state, next_iteration, stagnant_iterations)}")
    else:
        control_lines.append("- This iteration's angle: inspect current files/diff, make one concrete improvement, then verify.")
    control_lines.extend(
        [
            "- Do not repeat a no-op. If blocked, write a blocker note with the smallest next unblocker.",
            "- Prefer measurable checks over vibes; use the configured verifier when available.",
            "---",
        ]
    )
    return prompt.rstrip() + "\n" + "\n".join(control_lines) + "\n", system_message


def main() -> int:
    cwd = Path.cwd()
    state_path = cwd / STATE_REL
    if not state_path.exists():
        return 0

    debug("state file found")
    hook_input = parse_hook_input()
    debug("hook input parsed")
    try:
        state = load_json(state_path)
    except Exception as exc:
        print(f"⚠️ Wiggum loop: state file is invalid JSON ({exc}); stopping.", file=sys.stderr)
        try:
            state_path.unlink()
        except OSError:
            pass
        return 0

    state_session = str(state.get("session_id") or "")
    hook_session = str(hook_input.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "")
    if state_session and hook_session and state_session != hook_session:
        return 0

    iteration = int(state.get("iteration") or 1)
    max_iterations = int(state.get("max_iterations") or 0)
    stuck_after = int(state.get("stuck_after") or 0)

    last_output = last_assistant_text(hook_input.get("transcript_path"))
    debug("last assistant text loaded")
    if promise_was_met(last_output, state.get("completion_promise")):
        archive = archive_state(cwd, state_path, state, "promise")
        append_log(cwd, {"event": "stop", "reason": "promise", "iteration": iteration, "archive": str(archive) if archive else None})
        print(f"✅ Wiggum loop: detected completion promise; archived state at {archive}")
        return 0

    debug("running verifier")
    verifier = run_command(
        str(state.get("success_command") or ""),
        cwd,
        int(state.get("success_timeout_seconds") or 25),
    )
    debug(f"verifier done: {verifier.get('exit_code')}")
    if verifier.get("configured") and verifier.get("exit_code") == 0:
        debug("archiving verifier success")
        archive = archive_state(cwd, state_path, state, "verifier")
        append_log(cwd, {"event": "stop", "reason": "verifier", "iteration": iteration, "archive": str(archive) if archive else None})
        print(f"✅ Wiggum loop: success command passed; archived state at {archive}")
        return 0

    if max_iterations > 0 and iteration >= max_iterations:
        archive = archive_state(cwd, state_path, state, "max-iterations")
        append_log(cwd, {"event": "stop", "reason": "max_iterations", "iteration": iteration, "archive": str(archive) if archive else None})
        print(f"🛑 Wiggum loop: max iterations ({max_iterations}) reached; archived state at {archive}")
        return 0

    debug("computing workspace hash")
    current_hash = _workspace_hash(cwd, wiggum_pathspec_stop_hook)
    debug("workspace hash computed")
    previous_hash = str(state.get("last_workspace_hash") or "")
    stagnant_iterations = int(state.get("stagnant_iterations") or 0)
    if previous_hash and current_hash == previous_hash:
        stagnant_iterations += 1
    else:
        stagnant_iterations = 0

    if stuck_after > 0 and stagnant_iterations >= stuck_after:
        state.update(
            {
                "last_workspace_hash": current_hash,
                "stagnant_iterations": stagnant_iterations,
                "last_verifier": verifier,
            }
        )
        debug("archiving stuck pause")
        archive = archive_state(cwd, state_path, state, "stuck")
        debug("writing stuck pause log")
        append_log(
            cwd,
            {
                "event": "pause",
                "reason": "stuck",
                "iteration": iteration,
                "stagnant_iterations": stagnant_iterations,
                "archive": str(archive) if archive else None,
            },
        )
        print(
            f"⏸️ Wiggum loop: paused after {stagnant_iterations} no-progress stop events; archived state at {archive}"
        )
        return 0

    next_iteration = iteration + 1
    state.update(
        {
            "iteration": next_iteration,
            "last_workspace_hash": current_hash,
            "stagnant_iterations": stagnant_iterations,
            "last_verifier": verifier,
            "updated_at": utc_now(),
        }
    )
    debug("writing continued state")
    atomic_write_json(state_path, state)

    debug("writing continue log")
    append_log(
        cwd,
        {
            "event": "continue",
            "iteration": next_iteration,
            "mode": state.get("mode") or "reflective",
            "stagnant_iterations": stagnant_iterations,
            "workspace_hash": current_hash,
            "verifier": verifier,
        },
    )

    debug("reading prompt")
    prompt = read_prompt(cwd, state)
    if not prompt.strip():
        archive = archive_state(cwd, state_path, state, "empty-prompt")
        print(f"⚠️ Wiggum loop: prompt file missing or empty; archived state at {archive}", file=sys.stderr)
        return 0

    reason, system_message = build_reason(prompt, state, next_iteration, stagnant_iterations, verifier)
    print(json.dumps({"decision": "block", "reason": reason, "systemMessage": system_message}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
