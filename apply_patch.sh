#!/usr/bin/env bash
set -euo pipefail

PATCH_DIR_NAME="patches"

usage() {
  echo "Usage: $0 [patch_file]" >&2
  echo "  With no arguments, applies all .patch files in the '${PATCH_DIR_NAME}' directory." >&2
  exit 1
}

if [[ $# -gt 1 ]]; then
  usage
fi

if ! command -v git >/dev/null 2>&1; then
  echo "Error: git command not found" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 command not found" >&2
  exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || echo "$SCRIPT_DIR")
PATCH_DIR="$REPO_ROOT/$PATCH_DIR_NAME"

abs_path() {
  python3 - "$1" <<'PY'
import os
import sys
print(os.path.abspath(sys.argv[1]))
PY
}

resolve_patch_path() {
  local input="$1"
  local candidate
  for candidate in \
    "$input" \
    "$SCRIPT_DIR/$input" \
    "$REPO_ROOT/$input" \
    "$SCRIPT_DIR/$PATCH_DIR_NAME/$input" \
    "$PATCH_DIR_NAME/$input" \
    "$PATCH_DIR/$input"; do
    if [[ -f "$candidate" ]]; then
      abs_path "$candidate"
      return 0
    fi
  done
  return 1
}

apply_patch_file() {
  local patch_path="$1"
  echo "Applying $patch_path"

  if git apply --reverse --check "$patch_path" >/dev/null 2>&1; then
    echo "Patch already applied; skipping $patch_path"
    return 0
  fi

  local check_output
  check_output=$(git apply --check "$patch_path" 2>&1)
  local check_status=$?

  if [[ $check_status -eq 0 ]]; then
    local apply_output
    apply_output=$(git apply "$patch_path" 2>&1)
    local apply_status=$?

    if [[ $apply_status -eq 0 ]]; then
      [[ -n "$apply_output" ]] && printf '%s\n' "$apply_output"
      return 0
    fi

    printf '%s\n' "$apply_output" >&2
    return $apply_status
  fi

  if git apply --reverse --check "$patch_path" >/dev/null 2>&1; then
    echo "Patch already applied; skipping $patch_path"
    return 0
  fi

  printf '%s\n' "$check_output" >&2
  return $check_status
}

cd "$REPO_ROOT"

if [[ $# -eq 1 ]]; then
  if ! patch_file=$(resolve_patch_path "$1"); then
    echo "Error: could not locate patch file '$1'" >&2
    exit 1
  fi
  apply_patch_file "$patch_file"
  exit 0
fi

if [[ ! -d "$PATCH_DIR" ]]; then
  echo "Error: no patch directory found at $PATCH_DIR" >&2
  exit 1
fi

shopt -s nullglob
patch_files=("$PATCH_DIR"/*.patch)
shopt -u nullglob

if [[ ${#patch_files[@]} -eq 0 ]]; then
  echo "No .patch files found in $PATCH_DIR; nothing to do." >&2
  exit 0
fi

for patch_file in "${patch_files[@]}"; do
  apply_patch_file "$patch_file"
done
