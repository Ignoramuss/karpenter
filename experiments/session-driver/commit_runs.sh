#!/bin/bash
# Commits the session's records as it goes, and pushes with retries: finished cells (directories with summary.json)
# and set-aside attempts (<run_id>.failed-<n>) of the session matrix and the H7 timing pass, a snapshot of each
# matrix's index.jsonl (the file itself with FINAL=1), the timing pass's set-aside pairs (experiments/h7-timing-split), and the driver's records: a copy of
# its log (session.log), the machine log and the cut decisions. experiments/runs/session/ is in .git/info/exclude
# (the runner writes there continuously), so its files are added with -f.
cd /home/user/karpenter
S=/tmp/claude-0/-home-user-karpenter/2126f28c-53c0-5a8a-b14d-70c0de9f8c9e/scratchpad
n=0
for m in experiments/runs/session experiments/runs/h7-timing; do
  [ -d "$m" ] || continue
  for d in "$m"/*/; do
    case "$d" in *.failed-*) ;; *) [ -f "$d/summary.json" ] || continue ;; esac
    if [ -z "$(git ls-files -- "$d" | head -1)" ] || [ -n "$(git status --porcelain --ignored -- "$d")" ]; then
      git add -f "$d"; n=$((n+1))
    fi
  done
  # The runner appends to index.jsonl with every finished cell, so the live file is not tracked during the session
  # (it would always show as modified); a snapshot is committed instead, and the final file goes in place at the end
  # (FINAL=1).
  if [ -f "$m/index.jsonl" ]; then
    cp "$m/index.jsonl" "experiments/session-driver/index-$(basename "$m").jsonl"
    [ "${FINAL:-0}" = 1 ] && git add -f "$m/index.jsonl"
  fi
done
# Cells h7_pairs.py moved to experiments/h7-timing-split leave the timing pass's tree: stage their removal.
git ls-files -- experiments/runs/h7-timing | while read -r f; do [ -e "$f" ] || git rm -q --cached -- "$f"; done
[ -d experiments/h7-timing-split ] && git add -f experiments/h7-timing-split
cp "$S/logs/session.log" experiments/session-driver/session.log 2>/dev/null
git add experiments/session-driver
if git diff --cached --quiet; then echo "nothing to commit"; exit 0; fi
total=$(ls experiments/runs/session/*/summary.json 2>/dev/null | wc -l)
git commit -q -m "data(session): $n more finished session cells ($total in all)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0121zoDUH9JNxVmhKDp7ev4A" || exit 1
for i in 1 2 3 4; do timeout 180 git push origin claude/new-session-03ukbe 2>&1 | tail -1 && exit 0; sleep $((2**i)); done
echo "push failed"
