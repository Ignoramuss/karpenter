#!/bin/bash
# Commits finished session cells (directories with summary.json) and the index; pushes with retries.
cd /home/user/karpenter
n=0
for d in experiments/runs/session/*/; do
  [ -f "$d/summary.json" ] || continue
  if [ -z "$(git ls-files -- "$d" | head -1)" ] || [ -n "$(git status --porcelain --ignored -- "$d")" ]; then git add -f "$d"; n=$((n+1)); fi
done
# index.jsonl changes with every finished cell; it is committed with -f at the end of the session.
if git diff --cached --quiet; then echo "nothing to commit"; exit 0; fi
total=$(ls experiments/runs/session/*/summary.json 2>/dev/null | wc -l)
git commit -q -m "data(session): $n more finished session cells ($total in all)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0121zoDUH9JNxVmhKDp7ev4A" || exit 1
for i in 1 2 3 4; do timeout 180 git push origin claude/new-session-03ukbe 2>&1 | tail -1 && exit 0; sleep $((2**i)); done
echo "push failed"
