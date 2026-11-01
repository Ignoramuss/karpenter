#!/usr/bin/env bash
# Runs the Exp 9 matrix (experiments/EXP9.md) group by group with the runner's resume loop, four cells at a time,
# and logs each start with the VM's boot time so runs can be tied to the machine they ran on.
# Usage: experiments/run_exp9.sh <csim binary built at experiments/exp9-binary.txt's commit>
set -euo pipefail
BIN=${1:?csim binary}
cd "$(dirname "$0")/.."
LOG=experiments/exp9-machine-log.txt
printf '%s start btime=%s binary=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(awk '/^btime/{print $2}' /proc/stat)" \
  "$(go version -m "$BIN" 2>/dev/null | sed -n 's/.*vcs.revision=//p')" >> "$LOG"
for g in exp9-n200 exp9-n1000; do
  until "$BIN" matrix --matrix experiments/matrix/exp9.yaml --only "$g" --workers 4 --max-wall 60m | tee -a experiments/runs/exp9-driver.log | grep -q ' 0 pending'; do :; done
done
printf '%s done\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG"
