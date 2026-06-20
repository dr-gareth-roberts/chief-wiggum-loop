# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added
- GitHub Actions CI: a test matrix (Python 3.9–3.12 × Ubuntu/macOS) running the
  full shell + pytest suite, plus a `ruff` + `mypy` lint job.
- `pyproject.toml` with project metadata and `ruff`/`mypy` configuration.
- Cross-platform `--notify`: Linux (`notify-send`) and Windows (PowerShell
  balloon) in addition to macOS (`osascript`); unsupported platforms no-op.
- Integration tests covering the isolated runner `main()` (promise-driven stop,
  concurrent best-of-N over worktrees) and the Stop-hook decision/JSON output.

### Changed
- Best-of-N now computes the per-iteration worktree baseline (tracked diff +
  untracked file list) once and shares it across candidates instead of
  recomputing per candidate.

### Fixed
- `--preset` silently dropped any field whose value matched an argparse default
  (e.g. `explore` ignored `candidates`/`agent_switch_every`, `cheap` ignored
  `summary_max_chars`). Presets now apply to every flag the user did not pass
  explicitly while still letting explicit flags win.
- `git worktree add` for concurrent best-of-N candidates is serialized with a
  lock so simultaneous workers can't race on shared repo metadata.

## [0.3.0] — 2026-05-31

### Added
- `/wiggum-doctor` runs preflight diagnostics for Python, git, Claude CLI access, writable Wiggum state, active loop state, and stuck archives before starting a long isolated run.
- `/wiggum-resume` restores the latest stuck isolated-loop archive and replays the original invocation so paused work can continue with the existing prompt, summary, and state choices.
- `--notify` adds best-effort macOS desktop notifications at terminal states. Notification failures, missing `osascript`, and non-macOS platforms are ignored so loop exits stay reliable.
- Startup operability flags: `--agent-retries N` for short transient agent failures and `--no-agent-validation` for cases where startup probing is intentionally undesirable.
- A new aggregate test harness runs both shell suites plus pytest, with additional smoke coverage for doctor, resume, and notify behavior.

### Changed
- README and slash-command docs now lead with `/wiggum-doctor`, `/wiggum-resume`, and `--notify`, with safety and stuck-cause visibility as the secondary reliability story.
- Shared JSON, archive, git, time, text, and workspace-hash helpers now live in `scripts/wiggum_core.py`, reducing duplication between the Stop-hook loop and isolated runner.

### Fixed
- `--explain` attaches per-round stuck-cause signals to iteration logs, and pause output always prints the signal dict so no-progress, verifier, timeout, refusal, and metric-missing causes are visible without rerunning.
- Safety fixes cover read-only `~/.wiggum/`, worktree sandbox preservation of untracked main-tree files, startup validation for `{prompt_file}` agent commands, scoped metric parsing, tag extraction, verifier-based `auto` acceptance, and resume persistence for global-lesson opt-in state.

## [0.2.1] — 2026-05-25

### Fixed
- `--sandbox worktree` (now the default in git repos) silently dropped untracked main-tree files from the candidate workspace because the seed patch was forbidden from running `git add -N` on the main index. The runner now copies untracked files directly into the worktree, leaving the main index untouched.
- Startup `--agent-command` validation passed a literal `{prompt_file}` token to the probe shell, causing valid commands of the documented `cmd {prompt_file}` form to fail the probe and abort the loop. The probe now substitutes a real temp file the same way `run_agent` does.
- `state` did not record the `--global-lessons` opt-in, so `/wiggum-resume` could not re-apply it when restoring a stuck archive. The runner now persists `global_enabled` in `state` and the resume script honors it.

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
