#!/usr/bin/env bash
# agent-delegate installer.
#   ./install.sh                                  install the `delegate` command into ~/.local/bin
#   ./install.sh --for claude,codex --project DIR also add agent instructions to a project
#   ./install.sh --for claude,codex,hermes --global   add them to your user-level agent config
# Targets: claude (Claude Code skill), codex (AGENTS.md), agents (AGENTS.md for any agent that reads it),
#          gemini (GEMINI.md), hermes (Hermes skill; global only). Safe to re-run.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
TARGETS=""
PROJECT=""
GLOBAL=0
DRY=0

usage() { sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --for) TARGETS="${2:?--for needs a value}"; shift 2 ;;
    --project) PROJECT="${2:?--project needs a path}"; shift 2 ;;
    --global) GLOBAL=1; shift ;;
    --bin-dir) BIN_DIR="${2:?}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown option: $1" >&2; usage 2 ;;
  esac
done
if [ -n "$TARGETS" ] && [ -z "$PROJECT" ] && [ "$GLOBAL" = 0 ]; then
  echo "--for needs --project DIR or --global" >&2; exit 2
fi

run() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else "$@"; fi; }

# 1. Python check (tomllib needs 3.11+).
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "agent-delegate needs Python 3.11+ (python3 is $(python3 -V 2>&1))" >&2; exit 1; }

# 2. `delegate` shim.
run mkdir -p "$BIN_DIR"
if [ "$DRY" = 1 ]; then echo "[dry-run] write $BIN_DIR/delegate"; else
  printf '#!/usr/bin/env bash\nexec python3 "%s/delegate.py" "$@"\n' "$HERE" > "$BIN_DIR/delegate"
  chmod +x "$BIN_DIR/delegate"
fi
echo "installed: $BIN_DIR/delegate -> $HERE/delegate.py"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "note: add $BIN_DIR to your PATH" ;; esac

# 3. Agent instructions.
add_snippet() {  # file: append or replace the marked block
  local file="$1"
  if [ "$DRY" = 1 ]; then echo "[dry-run] update $file"; return; fi
  mkdir -p "$(dirname "$file")"; touch "$file"
  python3 - "$file" "$HERE/integrations/AGENTS.snippet.md" <<'PY'
import re, sys
path, snippet = sys.argv[1], open(sys.argv[2], encoding="utf-8").read()
text = open(path, encoding="utf-8").read()
block = re.compile(r"<!-- agent-delegate:start -->.*?<!-- agent-delegate:end -->\n?", re.S)
text = block.sub(lambda _: snippet, text) if block.search(text) else (text.rstrip("\n") + "\n\n" if text.strip() else "") + snippet
open(path, "w", encoding="utf-8").write(text)
PY
  echo "updated: $file"
}
add_skill() {
  run mkdir -p "$1"
  run cp "$HERE/integrations/SKILL.md" "$1/SKILL.md"
  echo "installed skill: $1/SKILL.md"
}

IFS=',' read -r -a list <<< "$TARGETS"
for t in "${list[@]}"; do
  [ -z "$t" ] && continue
  if [ "$GLOBAL" = 1 ]; then
    case "$t" in
      claude) add_skill "$HOME/.claude/skills/agent-delegate" ;;
      codex)  add_snippet "${CODEX_HOME:-$HOME/.codex}/AGENTS.md" ;;
      gemini) add_snippet "$HOME/.gemini/GEMINI.md" ;;
      hermes) add_skill "${HERMES_HOME:-$HOME/.hermes}/skills/autonomous-ai-agents/agent-delegate" ;;
      agents) echo "skip agents: there is no global AGENTS.md; use --project" ;;
      *) echo "unknown target: $t" >&2; exit 2 ;;
    esac
  else
    [ -d "$PROJECT" ] || { echo "no such project: $PROJECT" >&2; exit 2; }
    case "$t" in
      claude) add_skill "$PROJECT/.claude/skills/agent-delegate" ;;
      codex|agents) add_snippet "$PROJECT/AGENTS.md" ;;
      gemini) add_snippet "$PROJECT/GEMINI.md" ;;
      hermes) echo "skip hermes: Hermes skills are per user/profile; use --global" ;;
      *) echo "unknown target: $t" >&2; exit 2 ;;
    esac
  fi
done

cat <<EOF

Next:
  1. Edit $HERE/caps.toml for your plans and models.
  2. Log in to the CLIs you use (codex login, claude auth login, copilot).
  3. delegate status        # quota + policy view
  4. delegate probe --model <model>   # confirm a model answers
EOF
