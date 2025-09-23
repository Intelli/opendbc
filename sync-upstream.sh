#!/bin/bash
set -euo pipefail

DEFAULT_REF="upstream/hkg-angle-steering-2025"

EXCLUDES=(
  'AGENTS.md'
  'sync-upstream.sh'
  'update.sh'
  'apply_patch.sh'
  'create_patch.sh'
  '.gitmodules'
  'patches'
  'opendbc_repo'
)

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

PRE_SYNC_REF="$(git rev-parse --verify HEAD)"

# Prefer upstream changes on overlap; excluded files get restored after.
if ! git merge --no-edit -X theirs "${TARGET_REF}"; then
  echo "Merge with '${TARGET_REF}' failed. Aborting merge; please resolve issues manually." >&2
  git merge --abort >/dev/null 2>&1 || true
  exit 1
fi

restore_args=(
  "--source=${TARGET_REF}"
  --staged
  --worktree
  --no-overlay
  --
  .
)

for path in "${EXCLUDES[@]}"; do
  restore_args+=(":(top,exclude)${path}")
done

git restore "${restore_args[@]}"

if [[ ${#EXCLUDES[@]} -gt 0 ]]; then
  git restore --source="${PRE_SYNC_REF}" --staged --worktree -- "${EXCLUDES[@]}"
fi

# Align submodules to upstream commit while keeping opendbc_repo managed separately.
submodules=()
while IFS= read -r submodule_path; do
  [[ -z "${submodule_path}" ]] && continue
  submodules+=("${submodule_path}")
done < <(python3 - <<'PY_SUBMODULES'
import configparser

cfg = configparser.RawConfigParser()
cfg.read('.gitmodules')
for section in cfg.sections():
    path = cfg.get(section, 'path', fallback='').strip()
    if path and path != 'opendbc_repo':
        print(path)
PY_SUBMODULES
)

if [[ ${#submodules[@]} -gt 0 ]]; then
  git submodule sync --recursive -- "${submodules[@]}"
  git submodule update --init --recursive --checkout -- "${submodules[@]}"
fi
#git restore --source="${TARGET_REF}" --staged --worktree --no-overlay .
#git clean -fd
#git commit -m "Sync upstream"
#git push origin hkg-angle-steering-2025
