#!/usr/bin/env bash
# Runs experiments/tierb/run_tierb.py with the environment setup.sh wrote, from the repository root. Arguments pass
# through: "run" starts or resumes the evaluation cells, and is safe to re-invoke after an interruption, since the
# driver skips finished cells and reruns interrupted ones. For a long run, detach it:
#   setsid nohup experiments/tierb/env/run-driver.sh run >> /tmp/tierb/driver.log 2>&1 < /dev/null &
set -euo pipefail
export TIERB_HOME=${TIERB_HOME:-/tmp/tierb}
# shellcheck source=/dev/null
. "$TIERB_HOME/env.sh"
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
# One line per boot in the machine log, before the first cell on that boot (controller CPU depends on the machine).
boot=$(cat /proc/sys/kernel/random/boot_id)
if [ "${1:-}" = run ] && ! grep -q "boot_id=$boot" experiments/tierb/machine-log.txt; then
  printf '%s boot_id=%s btime=%s cpu="%s" nproc=%s\n' "$(date -u +%FT%TZ)" "$boot" \
    "$(date -u -d "@$(awk '/^btime/ {print $2}' /proc/stat)" +%FT%TZ)" \
    "$(lscpu | sed -n 's/^Model name: *//p')" "$(nproc)" >> experiments/tierb/machine-log.txt
fi
exec python3 experiments/tierb/run_tierb.py "$@"
