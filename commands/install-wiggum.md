---
description: "Install/copy the Wiggum Loop plugin bundle locally"
allowed-tools: ["Bash(${CLAUDE_PLUGIN_ROOT}/scripts/install-wiggum-plugin.sh:*)"]
---

# Install Wiggum Loop

Run the installer with the user's arguments:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/install-wiggum-plugin.sh" $ARGUMENTS
```

Common usage:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/install-wiggum-plugin.sh" --force
```

Optional best-effort settings update:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/install-wiggum-plugin.sh" --force --enable
```

The installer copies the local plugin bundle to `~/.claude/plugins/local/wiggum-loop` by default and prints next steps.
