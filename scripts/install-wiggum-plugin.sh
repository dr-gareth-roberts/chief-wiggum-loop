#!/usr/bin/env bash
# Install/copy the Wiggum Loop plugin bundle to a local Claude plugins directory.
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="$HOME/.claude/plugins/local/wiggum-loop"
ENABLE=false
FORCE=false

usage() {
  cat <<'EOF'
Install Wiggum Loop plugin bundle

USAGE:
  install-wiggum-plugin.sh [--target DIR] [--enable] [--force]

OPTIONS:
  --target DIR   Destination directory (default: ~/.claude/plugins/local/wiggum-loop)
  --enable       Best-effort update of ~/.claude/settings.json enabledPlugins
  --force        Replace an existing target directory
  -h, --help     Show help

This script copies the plugin files. Claude Code plugin discovery may vary by
version; if local plugin discovery does not pick it up automatically, use the
copied command/hook files directly or register the local plugin path manually.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      TARGET_DIR="${2:-}"
      [[ -n "$TARGET_DIR" ]] || { echo "--target requires a directory" >&2; exit 1; }
      shift 2
      ;;
    --enable)
      ENABLE=true
      shift
      ;;
    --force)
      FORCE=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -e "$TARGET_DIR" ]]; then
  if [[ "$FORCE" != true ]]; then
    echo "Target exists: $TARGET_DIR" >&2
    echo "Use --force to replace it." >&2
    exit 1
  fi
  rm -rf "$TARGET_DIR"
fi

mkdir -p "$(dirname "$TARGET_DIR")"
export SOURCE_DIR TARGET_DIR
python3 - <<'PY'
import os, shutil
from pathlib import Path
src = Path(os.environ['SOURCE_DIR'])
dst = Path(os.environ['TARGET_DIR'])
ignore = shutil.ignore_patterns('.git', '__pycache__', '*.pyc', '.DS_Store')
shutil.copytree(src, dst, ignore=ignore)
for path in dst.rglob('*.sh'):
    path.chmod(path.stat().st_mode | 0o111)
for path in [dst / 'hooks' / 'wiggum_stop_hook.py', dst / 'scripts' / 'wiggum_isolated_loop.py']:
    if path.exists():
        path.chmod(path.stat().st_mode | 0o111)
PY

echo "Installed Wiggum Loop plugin bundle to: $TARGET_DIR"

if [[ "$ENABLE" == true ]]; then
  SETTINGS="$HOME/.claude/settings.json"
  if [[ ! -f "$SETTINGS" ]]; then
    echo "No settings file found at $SETTINGS; skipping --enable." >&2
  else
    BACKUP="$SETTINGS.backup.$(date +%Y%m%d%H%M%S)"
    cp "$SETTINGS" "$BACKUP"
    python3 - "$SETTINGS" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text())
data.setdefault('enabledPlugins', {})['wiggum-loop@local'] = True
path.write_text(json.dumps(data, indent=2, sort_keys=False) + '\n')
PY
    echo "Best-effort enabled wiggum-loop@local in $SETTINGS"
    echo "Backup: $BACKUP"
  fi
fi

cat <<EOF

Next steps:
  - Restart Claude Code if plugin discovery requires restart.
  - Try /wiggum-isolated or run directly:
    $TARGET_DIR/scripts/wiggum-isolated-loop.sh "Fix tests" --success-command "npm test"
EOF
