#!/bin/bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <name>" >&2
  exit 1
fi

script_dir="$(cd "$(dirname "$0")" && pwd)"
cd "$script_dir"

patch_name="$1"
patch_dir="$script_dir/patches"
mkdir -p "$patch_dir"

if [[ $patch_name == *.patch ]]; then
  patch_path="$patch_dir/$patch_name"
else
  patch_path="$patch_dir/$patch_name.patch"
fi

if git diff --cached --quiet; then
  echo "No staged changes to create patch from." >&2
  exit 1
fi

if [[ -e "$patch_path" ]]; then
  echo "Patch file $patch_path already exists." >&2
  exit 1
fi

git diff --cached -R > "$patch_path"

echo "Created inverse patch at $patch_path"
