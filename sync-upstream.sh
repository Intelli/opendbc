git fetch upstream hkg-angle-steering-2025 --prune
git restore --source=upstream/hkg-angle-steering-2025 --staged --worktree --no-overlay -- . ':(glob,exclude)**/AGENTS.md' ':(glob,exclude)**/sync-upstream.sh'
#git restore --source=upstream/hkg-angle-steering-2025 --staged --worktree --no-overlay .
#git clean -fd
#git commit -m "Sync upstream"
#git push origin hkg-angle-steering-2025
