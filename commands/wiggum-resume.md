---
description: "Resume the most recent stuck Wiggum isolated loop"
allowed-tools: ["Bash(${CLAUDE_PLUGIN_ROOT}/scripts/wiggum-resume.sh)"]
---

# Wiggum Resume

Run:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/wiggum-resume.sh" $ARGUMENTS
```

Picks the most recent `.claude/wiggum-archive/wiggum-isolated.stuck.*.json`, restores it, and continues the loop with the original prompt and accumulated summary intact.
