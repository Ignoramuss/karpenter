#!/usr/bin/env python3
"""Preregistered time-based cut rule (experiments/PREREGISTRATION.md, "Run order, stopping and cuts").

A cut-candidate group starts only if now + (pending cells x median wall time of finished session cells of the same
scenario and scale) / 4 ends within 30 h of the first session cell's start. Fallback, logged before any W5/W6 cell
ran: with no finished session cell of that scenario and scale, the median is over finished session cells of the
same scale. Prints "START ..." or "CUT ..." with the arithmetic; exit 0 = START, 3 = CUT. Any other exit status
is an error (a crash, a bad ids file), which the driver must never read as a cut. An unreadable summary.json (a cell
being written, or one a VM replacement truncated) is skipped with a warning on stderr.
"""
import glob, json, os, statistics, sys
from datetime import datetime, timedelta, timezone

RUNS = "/home/user/karpenter/experiments/runs/session"
START = datetime(2026, 10, 27, 20, 52, 41, tzinfo=timezone.utc)
DEADLINE = START + timedelta(hours=30)
# (scenario, nodes) of each candidate group's cells, from experiments/matrix/session.yaml.
GROUPS = {
    "realism-w5": {("W5", 200): 60},
    "realism-w6": {("W6", 200): 60},
    "exp7-b2-n1000": {("W1", 1000): 10},
    "exp1-baselines-n1000": {("W1", 1000): 20, ("W2", 1000): 20, ("W4", 1000): 20},
}

def main(group, ids_file):
    ids = [l.strip() for l in open(ids_file) if l.strip()]
    want = GROUPS[group]
    assert len(ids) == sum(want.values()), (group, len(ids))
    walls, pending = {}, {}
    for f in glob.glob(os.path.join(RUNS, "*", "summary.json")):
        try:
            with open(f) as fh:
                s = json.load(fh)
            key, wall = (s["scenario"], s["scenario_nodes"]), float(s["wall_seconds"])
        except (OSError, ValueError, KeyError, TypeError) as err:
            print(f"warning: skipping {f}: {type(err).__name__}: {err}", file=sys.stderr)
            continue
        walls.setdefault(key, []).append(wall)
    for rid in ids:
        done = os.path.exists(os.path.join(RUNS, rid, "summary.json"))
        if done:
            continue
        key = next(k for k in want if len(want) == 1 or f"scenario-{k[0].lower()}_" in rid)
        pending[key] = pending.get(key, 0) + 1
    total, parts = 0.0, []
    for key, n in sorted(pending.items()):
        same = walls.get(key, [])
        basis = "same scenario and scale"
        if not same:
            same = [w for (sc, nodes), ws in walls.items() if nodes == key[1] for w in ws]
            basis = f"fallback: all {key[1]}-node cells"
        med = statistics.median(same)
        total += n * med
        parts.append(f"{key[0]}@{key[1]}: {n} cells x median {med/60:.1f} min ({basis}, n={len(same)})")
    now = datetime.now(timezone.utc)
    end = now + timedelta(seconds=total / 4)
    verdict = "START" if end <= DEADLINE else "CUT"
    print(f"{verdict} {group}: projected end {end:%Y-%m-%dT%H:%MZ} vs deadline {DEADLINE:%Y-%m-%dT%H:%MZ} "
          f"(now {now:%Y-%m-%dT%H:%MZ}; {'; '.join(parts) or 'no pending cells'}; /4 workers)")
    return 0 if verdict == "START" else 3

if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
