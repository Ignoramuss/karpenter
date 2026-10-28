#!/bin/bash
# Runs the session matrix group by group in the preregistered order. Each group is re-invoked until the runner
# reports "0 pending". Progress lines go to stdout (redirected to logs/session.log by the caller).
set -u
BIN="$1"   # simulator binary built at the Gate 3 commit
cd /home/user/karpenter
GROUPS_ORDER="exp1-n200 exp1-baselines-n200 exp1-n1000 exp6 exp6-baselines exp6-b5 exp5 exp5-steered exp3 exp4 exp7-n50 exp7-b2 abl exp8 exp8-b5 realism-w5 realism-w6 exp1-baselines-n1000 exp7-b2-n1000"
SKIP_FILE=/tmp/claude-0/-home-user-karpenter/2126f28c-53c0-5a8a-b14d-70c0de9f8c9e/scratchpad/session.skip
for g in $GROUPS_ORDER; do
  if [ -f "$SKIP_FILE" ] && grep -qx "$g" "$SKIP_FILE"; then echo "$(date -u +%FT%TZ) SKIP $g"; continue; fi
  echo "$(date -u +%FT%TZ) START $g"
  errors=0
  while true; do
    out=$("$BIN" matrix --matrix experiments/matrix/session.yaml --only "$g" --workers 4 --max-wall 60m 2>&1)
    echo "$out" | grep -E "^\[|^matrix" | tail -n 200
    last=$(echo "$out" | grep "^matrix" | tail -1)
    if [ -z "$last" ]; then
      errors=$((errors+1)); echo "$(date -u +%FT%TZ) RUNNER ERROR $g ($errors): $(echo "$out" | tail -3)"
      if [ $errors -ge 3 ]; then echo "$(date -u +%FT%TZ) GIVING UP $g"; break; fi
      sleep 60; continue
    fi
    if echo "$last" | grep -q " 0 pending"; then break; fi
    if [ -f "$SKIP_FILE" ] && grep -qx "$g" "$SKIP_FILE"; then echo "$(date -u +%FT%TZ) SKIP (mid-group) $g"; break; fi
  done
  echo "$(date -u +%FT%TZ) DONE $g :: $last"
done
echo "$(date -u +%FT%TZ) SESSION COMPLETE"
