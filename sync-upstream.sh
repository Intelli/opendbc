#!/bin/bash
set -euo pipefail

DEFAULT_REF="upstream/hkg-angle-steering-2025"

if [[ $# -gt 1 ]]; then
  echo "Usage: $0 [commit|ref]" >&2
  exit 1
fi

TARGET_REF="${1:-$DEFAULT_REF}"

git fetch upstream hkg-angle-steering-2025 --prune

if ! git rev-parse --verify "${TARGET_REF}^{commit}" >/dev/null 2>&1; then
  echo "Unable to resolve '${TARGET_REF}' to a commit. Did you fetch the right branch?" >&2
  exit 1
fi

git restore --source="${TARGET_REF}" --staged --worktree --no-overlay -- \
  . \
  ':(top,exclude)AGENTS.md' \
  ':(top,exclude)sync-upstream.sh' \
  ':(top,exclude)update.sh' \
  ':(top,exclude)apply_patch.sh' \
  ':(top,exclude)create_patch.sh' \
  ':(top,exclude).gitmodules' \
  ':(top,exclude)patches' \
  ':(top,exclude)opendbc_repo'
#git restore --source="${TARGET_REF}" --staged --worktree --no-overlay .
#git clean -fd
#git commit -m "Sync upstream"
#git push origin hkg-angle-steering-2025
