#!/bin/bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 <name> [update]" >&2
  exit 1
fi

script_dir="$(cd "$(dirname "$0")" && pwd)"
cd "$script_dir"

patch_name="$1"
update_mode="false"
if [[ ${2:-} == "update" || ${2:-} == "-u" || ${2:-} == "--update" ]]; then
  update_mode="true"
fi
patch_dir="$script_dir/patches"
mkdir -p "$patch_dir"

if [[ $patch_name == *.patch ]]; then
  patch_path="$patch_dir/$patch_name"
else
  patch_path="$patch_dir/$patch_name.patch"
fi

if [[ "$update_mode" != "true" ]]; then
  if [[ -e "$patch_path" ]]; then
    echo "Patch file $patch_path already exists." >&2
    exit 1
  fi
fi

if ! command -v git >/dev/null 2>&1; then
  echo "Error: git command not found" >&2
  exit 1
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Error: not inside a git repository" >&2
  exit 1
fi

# Ensure clean working tree and index before starting
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Error: working tree or index not clean. Please commit/stash changes before creating a patch." >&2
  exit 1
fi

echo "[1/6] Syncing upstream..."
"$script_dir/sync-upstream.sh"

if [[ "$update_mode" == "true" ]]; then
  echo "Deleting existing patch (if present): $patch_path"
  rm -f "$patch_path"
fi

echo "[2/6] Applying existing patches..."
"$script_dir/apply_patch.sh"

echo "[3/6] Staging applied changes..."
git add -A

if git diff --cached --quiet; then
  echo "No staged changes after applying patches; nothing to create." >&2
  exit 1
fi

echo "[4/6] Creating inverse patch at $patch_path"
# Exclude patch files from the generated diff to avoid self-referential diffs
git diff --cached -R -- . ':(exclude)patches/*.patch' > "$patch_path"

echo "[5/6] Verifying patches can be applied (idempotent check)..."
"$script_dir/apply_patch.sh"

echo "[6/6] Staging new patch file..."
git add "$patch_path"

echo "Done. Created inverse patch at $patch_path and staged it."
