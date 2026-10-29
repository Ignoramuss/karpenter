#!/bin/bash
# Session driver v4 (experiments/session-driver/README.md): v3 with the last recorded decision for a group taking
# effect. v3 was v2 plus: cut decisions recorded once, before acting, in
# cut-decisions.txt and followed on every restart (a group that started runs to completion; a crash of the cut-rule
# script is an error, never a cut); the VM (boot ID, boot time, CPU) logged in machine-log.txt before every runner
# invocation on a new boot; and the revised H7 timing pass (one invocation, interleaved arms, fillers, no wall
# budget; seeds whose arms started on different VMs are set aside and rerun). Waits for the previous driver (pid $2)
# to exit first, so one matrix runner holds a matrix lock at a time. After a VM replacement, restart it alone:
#   setsid nohup experiments/session-driver/run_session_v3.sh <binary> >> <log> 2>&1 < /dev/null &
set -u
BIN="$1"; OLD="${2:-}"
R=/home/user/karpenter
D=$R/experiments/session-driver
S=/tmp/claude-0/-home-user-karpenter/2126f28c-53c0-5a8a-b14d-70c0de9f8c9e/scratchpad
DECISIONS=$D/cut-decisions.txt
MACHINE_LOG=$D/machine-log.txt
SKIP_FILE=$S/session2.skip   # manual override only; v1 reads session.skip
cd $R
log_machine() {
  local id; id=$(cat /proc/sys/kernel/random/boot_id)
  if ! grep -q "boot_id=$id" "$MACHINE_LOG" 2>/dev/null; then
    echo "$(date -u +%FT%TZ) boot_id=$id btime=$(date -u -d @"$(awk '/btime/{print $2}' /proc/stat)" +%FT%TZ) cpu=\"$(lscpu | grep 'Model name' | sed 's/.*: *//')\" nproc=$(nproc)" >> "$MACHINE_LOG"
  fi
}
run_group() { # matrix file, --only value, label, wall budget per invocation
  local m="$1" only="$2" label="$3" wall="$4" errors=0 out last=""
  echo "$(date -u +%FT%TZ) START $label"
  while true; do
    log_machine
    out=$("$BIN" matrix --matrix "$m" --only "$only" --workers 4 --max-wall "$wall" 2>&1)
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
# The decision recorded for a candidate group: START, CUT or "" (none yet).
# The last decision recorded for a group wins: a decision is superseded only by a later logged one (RESEARCH_LOG,
# "Overrun rule for a started cut candidate").
recorded() { awk -v g="$1" '$3 == g && ($2 == "START" || $2 == "CUT") {d = $2} END {print d}' "$DECISIONS" 2>/dev/null; }
decide() { # group -> records START or CUT (exit 0) or fails (exit 1) without recording
  local g="$1" line="" rc="" i
  if [ "$cut_rest" -eq 1 ]; then echo "$(date -u +%FT%TZ) CUT $g :: an earlier cut candidate was cut" >> "$DECISIONS"; return 0; fi
  if compgen -G "experiments/runs/session/${g}__*" > /dev/null; then
    echo "$(date -u +%FT%TZ) START $g :: already started before this decision was recorded" >> "$DECISIONS"; return 0
  fi
  for i in 1 2 3; do
    line=""; rc=""
    if "$BIN" matrix --matrix experiments/matrix/session.yaml --only "$g" --dry-run > "$S/ids-$g.txt" && [ -s "$S/ids-$g.txt" ]; then
      line=$(python3 "$D/cut_projection.py" "$g" "$S/ids-$g.txt"); rc=$?
      if [ $rc -eq 0 ] || [ $rc -eq 3 ]; then
        echo "$(date -u +%FT%TZ) $([ $rc -eq 0 ] && echo START || echo CUT) $g :: $line" >> "$DECISIONS"; return 0
      fi
    fi
    echo "$(date -u +%FT%TZ) CUT-RULE ERROR $g (attempt $i): rc=${rc:-dry-run failed} $line"; sleep 60
  done
  return 1
}
if [ -n "$OLD" ]; then while kill -0 "$OLD" 2>/dev/null; do sleep 20; done; fi
log_machine
echo "$(date -u +%FT%TZ) DRIVER v3 takes over (boot $(cat /proc/sys/kernel/random/boot_id), cpu: $(lscpu | grep 'Model name' | sed 's/.*: *//'))"
GROUPS_ORDER="exp1-n200 exp1-baselines-n200 exp1-n1000 exp6 exp6-baselines exp6-b5 exp5 exp5-steered exp3 exp4 exp7-n50 exp7-b2 abl exp8 exp8-b5 b4 realism-w5 realism-w6 exp7-b2-n1000 exp1-baselines-n1000"
CANDIDATES=" realism-w5 realism-w6 exp7-b2-n1000 exp1-baselines-n1000 "
cut_rest=0
for g in $GROUPS_ORDER; do
  if [ -f "$SKIP_FILE" ] && grep -qx "$g" "$SKIP_FILE"; then echo "$(date -u +%FT%TZ) SKIP $g"; continue; fi
  if [[ "$CANDIDATES" == *" $g "* ]]; then
    if [ -z "$(recorded "$g")" ] && ! decide "$g"; then
      echo "$(date -u +%FT%TZ) BLOCKED: the cut rule could not be evaluated for $g; stopping (no cut recorded)"; exit 1
    fi
    echo "$(date -u +%FT%TZ) CUT-RULE $(grep -E "^[^ ]+ (START|CUT) $g( |$)" "$DECISIONS" | tail -1)"
    if [ "$(recorded "$g")" = CUT ]; then cut_rest=1; continue; fi
  fi
  run_group experiments/matrix/session.yaml "$g" "$g" 60m
done
echo "$(date -u +%FT%TZ) SESSION COMPLETE"
H7_ONLY="h7t-101,h7t-102,h7t-103,h7t-104,h7t-105,h7t-106,h7t-107,h7t-108,h7t-109,h7t-110,h7fill-111,h7fill-112"
for round in 0 1 2 3; do
  run_group experiments/matrix/h7-timing.yaml "$H7_ONLY" "h7-timing" 0s
  out=$(python3 "$D/h7_pairs.py" $([ $round -eq 3 ] && echo --report-only)); rc=$?
  echo "$(date -u +%FT%TZ) H7 PAIRS (round $round) rc=$rc :: $out"
  [ $rc -eq 4 ] || break
done
echo "$(date -u +%FT%TZ) H7 TIMING PASS COMPLETE"
cd pkg/controllers/disruption/pipeline
for cell in "WhenEmptyOrUnderutilized assorted wour-assorted" "Balanced kwok balanced-kwok" "Balanced assorted balanced-assorted"; do
  set -- $cell
  [ -s "$R/experiments/parity/chunks/parity-10000-seed2-$3.summary.txt" ] && continue
  echo "$(date -u +%FT%TZ) PARITY START $3"
  PARITY_STATES=10000 PARITY_SEED=2 PARITY_POLICIES=$1 PARITY_CATALOGS=$2 \
    PARITY_OUT=$R/experiments/parity/chunks/parity-10000-seed2-$3.jsonl \
    $S/bin/pipeline.test -test.run TestParity -test.count 1 -test.timeout 30h -test.v > $S/logs/parity-$3.log 2>&1
  rc=$?
  n=$(grep -c 'PARITY policy' $S/logs/parity-$3.log); want=$([ "$1" = Balanced ] && echo 3 || echo 2)
  echo "$(date -u +%FT%TZ) PARITY DONE $3 rc=$rc :: $n of $want configs"
  # A complete run writes one line per config, pass or fail (a mismatch fails the test after all states ran).
  if [ "$n" -ge "$want" ]; then
    { grep -o 'PARITY policy.*' $S/logs/parity-$3.log | sed 's/^PARITY //'; echo "test_exit_status=$rc"; } \
      > $R/experiments/parity/chunks/parity-10000-seed2-$3.summary.txt
    gzip -f $R/experiments/parity/chunks/parity-10000-seed2-$3.jsonl
  fi
done
echo "$(date -u +%FT%TZ) ALL BACKGROUND WORK COMPLETE"
