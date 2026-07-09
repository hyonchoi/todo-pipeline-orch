#!/usr/bin/env bash
# Install todos-manager skill from project source to user-level skill directories.
# Usage: scripts/install-todos-manager.sh
#
# Detects which skill directories exist (~/.claude/skills, ~/.agents/skills)
# and creates symlinks so the agent client can discover the skill.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SOURCE_SKILL="$PROJECT_ROOT/skills/todos-manager/SKILL.md"

# Verify source exists
if [[ ! -f "$SOURCE_SKILL" ]]; then
  echo "Error: Source skill not found at $SOURCE_SKILL"
  echo "Remediation: Ensure skills/todos-manager/SKILL.md exists in the project root."
  exit 1
fi

INSTALLED=0

install_to() {
  local target_dir="$1"
  local target_link="$target_dir/todos-manager/SKILL.md"

  # Skip if directory doesn't exist
  if [[ ! -d "$target_dir" ]]; then
    echo "  Skip $target_dir (not found)"
    return 0
  fi

  # Create per-skill directory if missing
  mkdir -p "$target_dir/todos-manager"

  # Remove existing file or symlink
  if [[ -e "$target_link" || -L "$target_link" ]]; then
    echo "  Updating existing link: $target_link"
    rm -f "$target_link"
  fi

  # Create symlink
  ln -sf "$SOURCE_SKILL" "$target_link"
  echo "  ✓ Linked: $target_link → $SOURCE_SKILL"
  INSTALLED=$((INSTALLED + 1))
}

echo "Installing todos-manager skill..."
echo "  Source: $SOURCE_SKILL"
echo ""

# Claude Code user skills
install_to "$HOME/.claude/skills"

# Agents user skills
install_to "$HOME/.agents/skills"

echo ""
if [[ $INSTALLED -gt 0 ]]; then
  echo "✓ Installed to $INSTALLED location(s). Restart your agent client to discover the skill."
else
  echo "⚠ No skill directories found. Create ~/.claude/skills/ or ~/.agents/skills/ and re-run."
fi
