#!/usr/bin/env bash
# Runs experiments/tierb/run_tierb.py with the environment setup.sh wrote, from the repository root. Arguments pass
# through: "run" starts or resumes the evaluation cells, and is safe to re-invoke after an interruption, since the
# driver skips finished cells and reruns interrupted ones. For a long run, detach it:
#   setsid nohup experiments/tierb/env/run-driver.sh run >> /tmp/tierb/driver.log 2>&1 < /dev/null &
set -euo pipefail
TIERB_HOME=${TIERB_HOME:-/tmp/tierb}
# shellcheck source=/dev/null
. "$TIERB_HOME/env.sh"
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
exec python3 experiments/tierb/run_tierb.py "$@"
