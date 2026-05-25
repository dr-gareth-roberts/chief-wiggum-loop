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
  --max-iterations 12
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
  --max-iterations 24 \
  --mode variants
```

If the agent command needs a prompt-file argument instead of stdin, include `{prompt_file}`:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/wiggum-isolated-loop.sh" \
  --prompt-file PROMPT.md \
  --agent-command "claude --print < {prompt_file}" \
  --success-command "pytest"
```
