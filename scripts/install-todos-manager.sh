#!/usr/bin/env bash
# Install todos-manager skill from project source to user-level skill directories.
# Usage: scripts/install-todos-manager.sh
#
# Detects which skill directories exist (~/.claude/skills, ~/.agents/skills)
# and creates symlinks so the agent client can discover the skill.

set -euo pipefail

# Source guard
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  echo "Error: This script must be executed, not sourced."
  return 1 2>/dev/null || exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SOURCE_SKILL="$PROJECT_ROOT/skills/todos-manager/SKILL.md"
SOURCE_SECTIONS="$PROJECT_ROOT/skills/todos-manager/sections"

# Verify source exists
if [[ ! -f "$SOURCE_SKILL" ]]; then
  echo "Error: Source skill not found at $SOURCE_SKILL"
  echo "Remediation: Ensure skills/todos-manager/SKILL.md exists in the project root."
  exit 1
fi

if [[ ! -d "$SOURCE_SECTIONS" ]]; then
  echo "Error: Source sections directory not found at $SOURCE_SECTIONS"
  echo "Remediation: Ensure skills/todos-manager/sections/ exists in the project root."
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

  # Remove existing file, symlink, or directory
  if [[ -d "$target_link" ]]; then
    echo "  Error: $target_link is a directory — skipping"
    return 0
  elif [[ -e "$target_link" || -L "$target_link" ]]; then
    echo "  Updating existing link: $target_link"
    rm -f "$target_link"
  fi

  # Compute relative path from symlink location to source so the link works
  # regardless of where the repo is cloned or which worktree it lives in.
  local link_dir
  link_dir="$(dirname "$target_link")"
  local rel_path
  rel_path="$(python3 -c "import os.path; print(os.path.relpath('$SOURCE_SKILL', '$link_dir'))" 2>/dev/null \
    || perl -MFile::Spec -e "print File::Spec->abs2rel('$SOURCE_SKILL', '$link_dir')" 2>/dev/null \
    || echo "$SOURCE_SKILL")"

  # Create relative symlink
  ln -sf "$rel_path" "$target_link"
  echo "  ✓ Linked: $target_link → $rel_path"
  INSTALLED=$((INSTALLED + 1))
}

install_sections_to() {
  local target_dir="$1"
  local target_link="$target_dir/todos-manager/sections"

  # Skip if parent skill directory doesn't exist (install_to creates it, so
  # this only fires when install_to itself skipped/failed for target_dir).
  if [[ ! -d "$target_dir/todos-manager" ]]; then
    return 0
  fi

  # Remove existing file or symlink; refuse to touch a real (non-symlink) directory
  if [[ -L "$target_link" ]]; then
    rm -f "$target_link"
  elif [[ -e "$target_link" ]]; then
    echo "  Error: $target_link exists and is not a symlink — skipping"
    return 0
  fi

  local link_dir
  link_dir="$(dirname "$target_link")"
  local rel_path
  rel_path="$(python3 -c "import os.path; print(os.path.relpath('$SOURCE_SECTIONS', '$link_dir'))" 2>/dev/null \
    || perl -MFile::Spec -e "print File::Spec->abs2rel('$SOURCE_SECTIONS', '$link_dir')" 2>/dev/null \
    || echo "$SOURCE_SECTIONS")"

  ln -sf "$rel_path" "$target_link"
  echo "  ✓ Linked: $target_link → $rel_path"
}

echo "Installing todos-manager skill..."
echo "  Source: $SOURCE_SKILL"
echo ""

# Claude Code user skills
install_to "$HOME/.claude/skills"
install_sections_to "$HOME/.claude/skills"

# Agents user skills
install_to "$HOME/.agents/skills"
install_sections_to "$HOME/.agents/skills"

echo ""
if [[ $INSTALLED -gt 0 ]]; then
  echo "✓ Installed to $INSTALLED location(s). Restart your agent client to discover the skill."
else
  echo "⚠ No skill directories found. Create ~/.claude/skills/ or ~/.agents/skills/ and re-run."
fi
