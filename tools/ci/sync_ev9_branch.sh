#!/usr/bin/env bash

set -euo pipefail

if [[ -n "${CI:-}" ]]; then
  git config --global --add safe.directory "$(pwd)"
fi

read_remote_head() {
  local ref_name="$1"
  git ls-remote --heads origin "${ref_name}" | awk 'NR==1 {print $1}'
}

dev_sha_remote=$(read_remote_head "ev9-dev")
prod_sha_remote=$(read_remote_head "ev9")

if [[ -z "${dev_sha_remote}" || -z "${prod_sha_remote}" ]]; then
  echo "Unable to determine remote commit shas for ev9-dev or ev9" >&2
  exit 1
fi

git fetch --no-tags origin "+${prod_sha_remote}:refs/remotes/origin/ev9"
git fetch --no-tags origin "+${dev_sha_remote}:refs/remotes/origin/ev9-dev"

dev_sha=$(git rev-parse origin/ev9-dev)
prod_sha=$(git rev-parse origin/ev9)

dev_tree=$(git rev-parse "${dev_sha}^{tree}")
prod_tree=$(git rev-parse "${prod_sha}^{tree}")

if [[ "${dev_tree}" == "${prod_tree}" ]]; then
  echo "ev9 already matches ev9-dev tree; nothing to sync."
  exit 0
fi

author_name=${SYNC_AUTHOR_NAME:-"GitHub Actions"}
author_email=${SYNC_AUTHOR_EMAIL:-"github-actions[bot]@users.noreply.github.com"}

export GIT_AUTHOR_NAME="${author_name}"
export GIT_AUTHOR_EMAIL="${author_email}"
export GIT_COMMITTER_NAME="${author_name}"
export GIT_COMMITTER_EMAIL="${author_email}"

run_ref="${GITHUB_RUN_URL:-manual run}"; run_trailer="Run: ${run_ref}"

commit_msg=$(cat <<COMMIT
sync: ev9-dev @ ${dev_sha:0:12}

${run_trailer}
COMMIT
)

new_commit=$(git commit-tree "${dev_tree}" -p "${prod_sha}" -m "${commit_msg}")

echo "Pushing ${new_commit} to ev9 (tree ${dev_tree})"
git push origin "${new_commit}:ev9"

echo "Sync complete: ev9 now reflects ev9-dev (${dev_sha})"

