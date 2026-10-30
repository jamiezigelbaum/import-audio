#!/usr/bin/env bash
# Link the skill into each agent's skill folder and seed the owner's profile.
#
#   ./install.sh            # Claude Code and Codex
#   ./install.sh claude     # just one
#
# Existing files are never overwritten.
set -euo pipefail

SKILL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/skill/import-audio"
HOME_DIR=${AUDIO_IMPORT_HOME:-$HOME/.config/import-audio}
targets=${*:-claude codex}

link() {
  local dir=$1
  mkdir -p "$dir"
  if [ -e "$dir/import-audio" ] || [ -L "$dir/import-audio" ]; then
    echo "skip: $dir/import-audio already exists"
  else
    ln -s "$SKILL" "$dir/import-audio"
    echo "linked: $dir/import-audio"
  fi
}

for target in $targets; do
  case $target in
    claude) link "$HOME/.claude/skills" ;;
    codex) link "$HOME/.agents/skills" ;;
    *) echo "unknown target: $target (use claude or codex)" >&2; exit 2 ;;
  esac
done

mkdir -p "$HOME_DIR"
for name in config.toml routing.md vocabulary.md; do
  if [ -e "$HOME_DIR/$name" ]; then
    echo "skip: $HOME_DIR/$name already exists"
  else
    cp "$SKILL/examples/$name" "$HOME_DIR/$name"
    echo "created: $HOME_DIR/$name (edit it)"
  fi
done
