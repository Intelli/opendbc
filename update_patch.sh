#!/bin/bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <name>" >&2
  exit 1
fi

script_dir="$(cd "$(dirname "$0")" && pwd)"
exec "$script_dir/create_patch.sh" "$1" update


