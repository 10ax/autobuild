#!/usr/bin/env bash
# Review every quality branch that has landed: what changed, and did it stay in its lane.
set -uo pipefail
DATE="${1:-$(date +%F)}"
for R in ~/Personal/code/telegram-photo-vault ~/Personal/code/bash \
         ~/Personal/code/web/skv-web ~/Personal/code/web/10ax.github.io; do
  B="quality/$DATE"
  git -C "$R" rev-parse --verify --quiet "$B" >/dev/null || continue
  D=$(git -C "$R" rev-parse --abbrev-ref HEAD)
  echo "=============================================================="
  echo "$R   $D..$B"
  git -C "$R" log --format='  %h %an <%ae>  %s' -1 "$B"
  git -C "$R" diff --stat "$D..$B" | tail -20
  echo "  worktree left standing (red run): $([ -d ~/autobuild/state/worktrees/quality-* ] && ls -d ~/autobuild/state/worktrees/* 2>/dev/null | tr '\n' ' ' || echo none)"
  echo "  merge:  git -C $R merge --ff-only $B"
  echo "  drop:   git -C $R branch -D $B"
done
