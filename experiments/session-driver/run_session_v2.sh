#!/bin/bash
# Session driver, second version: the preregistered group order (experiments/PREREGISTRATION.md, "Run order,
# stopping and cuts"; v1 omitted b4 and swapped the last two groups), the time-based cut rule applied before each
# cut-candidate group, then the supplementary H7 timing pass and the remaining 10,000-state parity chunks.
# Waits for the v1 driver (pid $2) to exit first, so one matrix runner holds the lock at a time.
set -u
BIN="$1"; OLD="${2:-}"
S=/tmp/claude-0/-home-user-karpenter/2126f28c-53c0-5a8a-b14d-70c0de9f8c9e/scratchpad
cd /home/user/karpenter
if [ -n "$OLD" ]; then while kill -0 "$OLD" 2>/dev/null; do sleep 20; done; fi
echo "$(date -u +%FT%TZ) DRIVER v2 takes over (cpu: $(lscpu | grep 'Model name' | sed 's/.*: *//'))"
GROUPS_ORDER="exp1-n200 exp1-baselines-n200 exp1-n1000 exp6 exp6-baselines exp6-b5 exp5 exp5-steered exp3 exp4 exp7-n50 exp7-b2 abl exp8 exp8-b5 b4 realism-w5 realism-w6 exp7-b2-n1000 exp1-baselines-n1000"
CANDIDATES=" realism-w5 realism-w6 exp7-b2-n1000 exp1-baselines-n1000 "
SKIP_FILE=$S/session2.skip   # v1 reads session.skip, which holds every group after exp1-n1000 so v1 exits there
cut_rest=0
run_group() { # matrix file, --only value, label
  local m="$1" only="$2" label="$3" errors=0 out last=""
  echo "$(date -u +%FT%TZ) START $label"
  while true; do
    out=$("$BIN" matrix --matrix "$m" --only "$only" --workers 4 --max-wall 60m 2>&1)
    echo "$out" | grep -E "^\[|^matrix" | tail -n 200
    last=$(echo "$out" | grep "^matrix" | tail -1)
    if [ -z "$last" ]; then
      errors=$((errors+1)); echo "$(date -u +%FT%TZ) RUNNER ERROR $label ($errors): $(echo "$out" | tail -3)"
      if [ $errors -ge 3 ]; then echo "$(date -u +%FT%TZ) GIVING UP $label"; break; fi
      sleep 60; continue
    fi
    if echo "$last" | grep -q " 0 pending"; then break; fi
    if [ -f "$SKIP_FILE" ] && grep -qx "$label" "$SKIP_FILE"; then echo "$(date -u +%FT%TZ) SKIP (mid-group) $label"; break; fi
  done
  echo "$(date -u +%FT%TZ) DONE $label :: $last"
}
for g in $GROUPS_ORDER; do
  if [ -f "$SKIP_FILE" ] && grep -qx "$g" "$SKIP_FILE"; then echo "$(date -u +%FT%TZ) SKIP $g"; continue; fi
  if [[ "$CANDIDATES" == *" $g "* ]]; then
    if [ $cut_rest -eq 1 ]; then echo "$(date -u +%FT%TZ) CUT $g: an earlier cut candidate was cut"; continue; fi
    "$BIN" matrix --matrix experiments/matrix/session.yaml --only "$g" --dry-run > "$S/ids-$g.txt"
    line=$(python3 "$S/cut_projection.py" "$g" "$S/ids-$g.txt"); rc=$?
    echo "$(date -u +%FT%TZ) CUT-RULE $line"
    if [ $rc -ne 0 ]; then cut_rest=1; continue; fi
  fi
  run_group experiments/matrix/session.yaml "$g" "$g"
done
echo "$(date -u +%FT%TZ) SESSION COMPLETE"
for p in "h7t-101,h7t-102" "h7t-103,h7t-104" "h7t-105,h7t-106" "h7t-107,h7t-108" "h7t-109,h7t-110"; do
  run_group experiments/matrix/h7-timing.yaml "$p" "$p"
done
echo "$(date -u +%FT%TZ) H7 TIMING PASS COMPLETE"
cd pkg/controllers/disruption/pipeline
for cell in "WhenEmptyOrUnderutilized assorted wour-assorted" "Balanced kwok balanced-kwok" "Balanced assorted balanced-assorted"; do
  set -- $cell
  echo "$(date -u +%FT%TZ) PARITY START $3"
  PARITY_STATES=10000 PARITY_SEED=2 PARITY_POLICIES=$1 PARITY_CATALOGS=$2 \
    PARITY_OUT=/home/user/karpenter/experiments/parity/chunks/parity-10000-seed2-$3.jsonl \
    $S/bin/pipeline.test -test.run TestParity -test.count 1 -test.timeout 30h -test.v > $S/logs/parity-$3.log 2>&1
  rc=$?
  echo "$(date -u +%FT%TZ) PARITY DONE $3 rc=$rc :: $(grep -c 'PARITY policy' $S/logs/parity-$3.log) configs"
  grep -o 'PARITY policy.*' $S/logs/parity-$3.log | sed 's/^PARITY //' > /home/user/karpenter/experiments/parity/chunks/parity-10000-seed2-$3.summary.txt
  gzip -f /home/user/karpenter/experiments/parity/chunks/parity-10000-seed2-$3.jsonl
done
echo "$(date -u +%FT%TZ) ALL BACKGROUND WORK COMPLETE"
