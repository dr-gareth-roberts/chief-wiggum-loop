# Changelog

All notable changes to this project will be documented in this file.

## [0.2.0] — 2026-05-25

### Breaking
- Global lessons are now opt-in. The default is project-only (`.claude/wiggum-lessons.jsonl`). Pass `--global-lessons` to also append to `~/.wiggum/lessons.jsonl`. The `--no-global-lessons` flag still works but is deprecated.
- `--sandbox` now defaults to auto-upgrade: in a git repo with HEAD, it becomes `worktree`; otherwise `none`. Pass `--sandbox none` explicitly to opt out.

### Fixed
- Crash under read-only `~/.wiggum/` (e.g., Claude Code sandbox): `append_jsonl` now returns instead of raising on `PermissionError`/`OSError`, and the loop records a one-shot `global_lessons_disabled` ledger event.
- `classify_stuck_reason` checked the refusal regex before stagnation, so subprocess errors mentioning "cannot find" were misclassified as model refusals. Stagnation now wins; refusal regex is checked last and against the last 800 chars of output only.
- `candidate_accepted(acceptance="auto")` silently degraded to `always` when `sandbox=none` and `candidates=1`, ignoring `--success-command`. It now uses `verifier` whenever `--success-command` is set.
- `<review>`/`<promise>` tag extraction used a fragile string-replace dance. Replaced with a single `extract_tagged()` helper.
- `mark_untracked_for_diff` mutated the user's git index via `git add -N` even when capturing the main tree. It now requires `allow_in_main=True` and is only enabled inside candidate sandboxes.
- `parse_metrics` accepted any `NAME=NUMBER` line, capturing stray env-var dumps. The bare form now requires `NAME` to equal the configured `--metric-name`.

### Added
- `--agent-retries N` (default 1): retry an agent invocation that exits non-zero in under 5 seconds, to weather transient rate-limit or network blips.
- `--no-agent-validation`: skip the startup probe that runs each `--agent-command` with a small input to verify it exists and is executable.
- `--explain`: attach a per-round `reason_signals` dict to the iteration log entry (regex_hit, stagnant, verifier_changed, agent_timeout, metric_missing), making stuck-reason provenance debuggable.
- The "stuck cause" signals dict is always printed alongside the `⏸️ Paused...` line, regardless of `--explain`.
