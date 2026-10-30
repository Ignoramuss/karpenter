#!/usr/bin/env bash
# Commits the Tier B cells that have finished (manifest status "done"), every set-aside attempt, the driver's log
# and plan, and pushes. experiments/runs/tierb-kwok/ is excluded locally, so a cell still running is never added.
set -euo pipefail
cd "$(dirname "$0")/../.."
n=0
for d in experiments/runs/tierb-kwok/*/; do
  case "$d" in
    # A set-aside attempt (.failed-N, .interrupted-<ts>, .infra-<ts>) is never running again, whatever its manifest
    # says: the driver renames a directory only when it is done with it.
    *.failed-*/ | *.interrupted-*/ | *.infra-*/) git add -f "$d"; continue ;;
  esac
  m="$d/manifest.json"
  [ -f "$m" ] && grep -q '"status": "done"' "$m" || continue
  git add -f "$d"
  n=$((n + 1))
done
git add experiments/tierb/machine-log.txt
git add experiments/tierb/run.log experiments/tierb/plan.json 2>/dev/null || true
if git diff --cached --quiet; then
  echo "nothing new to commit"
  exit 0
fi
git commit -q -m "data(tierb): Tier B KWOK cells, $n finished so far

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0121zoDUH9JNxVmhKDp7ev4A"
for i in 1 2 3 4; do git push -q -u origin claude/new-session-03ukbe && break || sleep $((2 ** i)); done
git log --oneline -1
