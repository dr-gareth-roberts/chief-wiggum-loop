# v0.3.0 Release Notes

Wiggum Loop 0.3.0 is a reliability and operability release for longer isolated runs.

## Headline features

- `/wiggum-doctor`: preflight diagnostics for Python, git, Claude CLI access, writable Wiggum state, active loop state, and stuck archives.
- `/wiggum-resume`: restores the latest stuck isolated-loop archive and continues with the original prompt, rolling summary, and persisted run choices.
- `--notify`: best-effort macOS desktop notifications when an isolated loop finishes, pauses, or stops on a budget. Missing `osascript` and non-macOS platforms are safe no-ops.

## Reliability and safety

- Startup validation now catches missing or broken `--agent-command` values before a long run begins.
- `--agent-retries N` retries short transient agent failures, while `--no-agent-validation` keeps an escape hatch for expensive or unusual worker commands.
- `--explain` records per-round stuck-cause signals in the iteration log, and pause output always prints the signal dict for quick diagnosis.
- Worktree sandboxing preserves untracked main-tree files without mutating the user's index.
- Read-only `~/.wiggum/` no longer crashes global-lesson writes; project-local lessons continue.
- Resume now preserves the original global-lessons opt-in choice.
- Shared `scripts/wiggum_core.py` helpers reduce duplicated archive, JSON, git, time, text, and workspace-hash logic across entry points.

## Tests

- The full harness now runs both shell suites plus pytest through `tests/run-all-tests.sh`.
- New smoke coverage verifies `/wiggum-doctor`, `/wiggum-resume` no-archive behavior, and safe `--notify` no-op behavior when notification delivery is unavailable.

## Upgrade notes

- Global lessons remain opt-in. Use `--global-lessons` to append to `~/.wiggum/lessons.jsonl`; project-local lessons are still written by default.
- In git repos with `HEAD`, the isolated runner auto-upgrades the default sandbox to `worktree`. Use `--sandbox none` only when direct main-tree edits are intentional.
