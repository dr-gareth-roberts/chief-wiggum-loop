---
description: "Start Wiggum Loop in the current session"
allowed-tools: ["Bash(${CLAUDE_PLUGIN_ROOT}/scripts/setup-wiggum-loop.sh:*)"]
---

# Wiggum Loop Command

Execute the setup script with the user's arguments:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/setup-wiggum-loop.sh" $ARGUMENTS
```

Then begin working on the emitted prompt. When you try to exit, the Stop hook will continue the loop until one of the configured stop conditions is met.

Differences from the basic Ralph loop:
- default finite max iterations unless `--allow-infinite` is set
- optional `--success-command` verifier can stop the loop automatically
- optional no-progress pause prevents silent spinning
- `reflective` and `variants` modes add iteration steering while preserving the same core prompt
