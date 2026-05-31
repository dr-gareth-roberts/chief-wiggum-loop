---
description: "Start Wiggum Loop as true isolated agent subprocesses"
allowed-tools: ["Bash(${CLAUDE_PLUGIN_ROOT}/scripts/wiggum-isolated-loop.sh:*)"]
---

# Wiggum Isolated Loop

Run the isolated orchestrator with the user's arguments:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/wiggum-isolated-loop.sh" $ARGUMENTS
```

Use this when the normal Stop-hook loop would accumulate too much chat context. Each iteration launches a fresh agent command and passes a bounded prompt through stdin. The runner also injects `.claude/wiggum-isolated-summary.local.md` by default so each fresh worker can see what previous rounds tried without inheriting the full chat.

Typical usage:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/wiggum-isolated-loop.sh" \
  "Fix the failing tests" \
  --agent-command "claude --print" \
  --success-command "npm test" \
  --max-iterations 12 \
  --notify
```

Multi-model batch + critic + final reviewer:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/wiggum-isolated-loop.sh" \
  "Make the tests pass without weakening assertions" \
  --agent-command "claude --print" \
  --agent-command "codex exec -" \
  --agent-switch-every 4 \
  --critic-command "gemini -p" \
  --critic-every 4 \
  --review-command "claude --print" \
  --success-command "npm test" \
  --agent-retries 1 \
  --max-iterations 24 \
  --mode variants \
  --explain \
  --notify
```

`--explain` attaches the per-round `reason_signals` dict (`regex_hit`, `stagnant`, `verifier_changed`, `agent_timeout`, `metric_missing`) to each iteration entry in `.claude/wiggum-isolated.log.jsonl`, which makes it obvious which signal classified a round as stuck when post-mortem-ing a long multi-model run.

Operational flags:

- `--notify`: best-effort macOS terminal-state notification; missing `osascript` is a safe no-op.
- `--agent-retries N`: retry short non-zero agent exits before treating the worker as failed.
- `--no-agent-validation`: skip the startup probe for deliberately unusual or expensive agent commands.
- `--explain`: write stuck-cause signal details into each iteration log entry.

If the agent command needs a prompt-file argument instead of stdin, include `{prompt_file}`:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/wiggum-isolated-loop.sh" \
  --prompt-file PROMPT.md \
  --agent-command "claude --print < {prompt_file}" \
  --success-command "pytest"
```
