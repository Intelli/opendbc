#!/usr/bin/env bash
# fix_patch.sh — launch Claude Code to manually apply a patch's changes to the current codebase.
# Usage: ./fix_patch.sh <patch_name>    (e.g., ./fix_patch.sh example.patch)

set -euo pipefail

usage() {
  echo "Usage: $0 <patch_name.patch>"
  echo "Example: $0 example.patch"
  exit 1
}

# Require a single argument
[[ $# -ge 1 ]] || usage

PATCH_NAME="$1"

# Ensure Claude Code CLI is available
if ! command -v claude >/dev/null 2>&1; then
  echo "Error: 'claude' CLI not found in PATH. Install Claude Code and add it to PATH, then retry."
  exit 1
fi

# Find repo root (fallback to current dir if not in a git repo)
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

# Patch must exist at patches/<patch_name>
PATCH_PATH="${REPO_ROOT}/patches/${PATCH_NAME}"
if [[ ! -f "$PATCH_PATH" ]]; then
  echo "Error: Patch file not found: patches/${PATCH_NAME}"
  echo "Looked in: ${PATCH_PATH}"
  exit 1
fi

# The exact prompt requested
PROMPT="I'm trying to apply the diff file in patches/${PATCH_NAME} to the codebase. However, it's no longer able to apply as the codebase has changed significantly. We've already tried applying the patch to the codebase. Review the unstaged/staged files in git, and identify any conflicts (e.g. "ours" vs "theirs"). Modify the files in the codebase to properly resolve any conflicts, making sure all the changes from the patch file are incorporated, while also keeping the newer codebase changes. Don't modify the .patch file itself, and don't run any git commands to stage/unstage files."

echo "→ Launching Claude Code from: ${REPO_ROOT}"
echo "→ Patch: patches/${PATCH_NAME}"
echo

# Replace this script process with Claude so it 'takes over' the terminal.
# --permission-mode acceptEdits = auto-approve file edits (similar to codex --full-auto).
cd "${REPO_ROOT}"
exec claude --permission-mode acceptEdits "${PROMPT}"
