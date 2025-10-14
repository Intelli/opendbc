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

list_conflicted_files() {
  git diff --name-only --diff-filter=U
}

apply_patch_file() {
  local patch_path="$1"
  echo "Applying $patch_path"

  local pre_conflicts
  pre_conflicts=$(list_conflicted_files)
  if [[ -n "$pre_conflicts" ]]; then
    echo "Error: unresolved merge conflicts detected before applying $patch_path" >&2
    echo "Conflicted files:" >&2
    while IFS= read -r conflict_file; do
      [[ -z "$conflict_file" ]] && continue
      echo "  $conflict_file" >&2
    done <<< "$pre_conflicts"
    return 1
  fi

  if git apply --reverse --check "$patch_path" >/dev/null 2>&1; then
    echo "Patch already applied; skipping $patch_path"
    return 0
  fi

  local check_output
  local check_status=0
  set +e
  check_output=$(git apply --check "$patch_path" 2>&1)
  check_status=$?
  set -e
  if [[ $check_status -ne 0 ]]; then
    echo "Standard apply failed; retrying with --3way" >&2
    local staged_before
    staged_before=$(git diff --name-only --cached || true)
    local three_way_output
    local three_way_status=0
    set +e
    three_way_output=$(git apply --3way --index "$patch_path" 2>&1)
    three_way_status=$?
    set -e

    if [[ $three_way_status -ne 0 ]]; then
      local post_conflicts
      post_conflicts=$(list_conflicted_files)
      if [[ -n "$post_conflicts" ]]; then
        echo "Patch requires manual conflict resolution: $patch_path" >&2
        [[ -n "$three_way_output" ]] && printf '%s\n' "$three_way_output" >&2
        echo "Conflicted files:" >&2
        while IFS= read -r conflict_file; do
          [[ -z "$conflict_file" ]] && continue
          echo "  $conflict_file" >&2
        done <<< "$post_conflicts"
        echo "Resolve the conflicts, then stage the files and re-run the script if needed." >&2
        return 1
      fi

      echo "Failed to apply $patch_path" >&2
      [[ -n "$check_output" ]] && printf '%s\n' "$check_output" >&2
      [[ -n "$three_way_output" ]] && printf '%s\n' "$three_way_output" >&2
      return $three_way_status
    fi

    local staged_after
    staged_after=$(git diff --name-only --cached || true)
    if [[ -n "$staged_after" ]]; then
      while IFS= read -r staged_file; do
        [[ -z "$staged_file" ]] && continue
        if ! grep -Fqx "$staged_file" <<< "$staged_before"; then
          git reset HEAD -- "$staged_file"
        fi
      done <<< "$staged_after"
    fi

    local post_conflicts
    post_conflicts=$(list_conflicted_files)
    if [[ -n "$post_conflicts" ]]; then
      echo "Patch applied with conflicts: $patch_path" >&2
      [[ -n "$three_way_output" ]] && printf '%s\n' "$three_way_output" >&2
      echo "Conflicted files:" >&2
      while IFS= read -r conflict_file; do
        [[ -z "$conflict_file" ]] && continue
        echo "  $conflict_file" >&2
      done <<< "$post_conflicts"
      echo "Resolve the conflicts, then stage the files and re-run the script if needed." >&2
      return 1
    fi

    [[ -n "$three_way_output" ]] && printf '%s\n' "$three_way_output"
    return 0
  fi

  local apply_output
  local apply_status=0
  set +e
  apply_output=$(git apply "$patch_path" 2>&1)
  apply_status=$?
  set -e
  if [[ $apply_status -ne 0 ]]; then
    echo "Failed to apply $patch_path" >&2
    [[ -n "$apply_output" ]] && printf '%s\n' "$apply_output" >&2
    return $apply_status
  fi

  [[ -n "$apply_output" ]] && printf '%s\n' "$apply_output"
  return 0
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
