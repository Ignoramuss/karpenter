#!/usr/bin/env python3
"""After the H7 timing pass: find seeds whose two arms started on different VMs and set them aside, together with
every filler cell, so the driver reruns them in one invocation at 4-way load (experiments/RESEARCH_LOG.md, "H7-S
design revision").

A run's VM is the last boot in machine-log.txt (btime=...) at or before its manifest's started_at; the driver logs
the boot ID, boot time and CPU before every runner invocation, so every boot a run started on is listed. Set-aside
cells go to experiments/h7-timing-split/round-<n>/ (outside experiments/runs, so no analysis reads them).
A seed is also rerun when one of its arms has a failed attempt (the runner retries it in a later invocation, at a
lower load). Seeds with an unfinished or abandoned arm are reported and left alone. With --report-only (the last
round) nothing is moved. Exit 0: nothing (more) to rerun; 4: cells were set aside, rerun the pass; 2: error (no
machine log).
"""
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUNS = REPO / "experiments" / "runs" / "h7-timing"
MACHINE_LOG = REPO / "experiments" / "session-driver" / "machine-log.txt"
ASIDE = REPO / "experiments" / "h7-timing-split"
SEEDS = range(101, 111)
ARMS = ("heuristic/entropy-greedy", "random/entropy-greedy")
_BTIME = re.compile(r"\bbtime=(\S+)")


def parse_time(text):
    """An RFC 3339 UTC timestamp with up to nanosecond precision."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z", text)
    if not m:
        raise ValueError(f"bad timestamp {text!r}")
    frac = (m.group(2) or ".0")[1:7].ljust(6, "0")
    return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(microsecond=int(frac), tzinfo=timezone.utc)


def boots(path=MACHINE_LOG):
    """Sorted boot times from the machine log; "btime=unknown" (the first VM's) sorts first."""
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _BTIME.search(line)
        if line.startswith("#") or not m:
            continue
        out.add(datetime.min.replace(tzinfo=timezone.utc) if m.group(1) == "unknown" else parse_time(m.group(1)))
    return sorted(out)


def epoch(started, boot_times):
    return max(i for i, b in enumerate(boot_times) if b <= started) if boot_times and boot_times[0] <= started else -1


def main(argv):
    report_only = "--report-only" in argv
    if not MACHINE_LOG.is_file():
        print(f"error: {MACHINE_LOG} not found")
        return 2
    bts = boots()
    arms, retried = {}, set()
    for d in sorted(RUNS.glob("h7t-*__*")):
        if ".failed-" in d.name:
            cell = d / "cell.json"
            if cell.is_file():
                cfg = json.loads(cell.read_text(encoding="utf-8"))
                retried.add(int(cfg["seed"]))
            continue
        if not (d / "summary.json").is_file() or not (d / "manifest.json").is_file():
            continue
        m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        cfg = m.get("config") or {}
        arms[(int(cfg["seed"]), (cfg.get("arm") or {}).get("label"))] = (epoch(parse_time(m["started_at"]), bts), d)
    complete = [s for s in SEEDS if all((s, a) in arms for a in ARMS)]
    unfinished = [(s, a) for s in SEEDS for a in ARMS if (s, a) not in arms]
    split = [s for s in complete if arms[(s, ARMS[0])][0] != arms[(s, ARMS[1])][0]]
    # A seed with a failed attempt was retried in a later runner invocation, at a lower load than its twin.
    rerun = sorted(set(split) | (retried & set(complete)))
    note = f"; unfinished or abandoned arms {unfinished}" if unfinished else ""
    if not rerun:
        print(f"no seed to rerun: {len(complete)} seeds with both arms on one VM and no retried attempt{note}")
        return 0
    if report_only:
        print(f"split or retried seeds {rerun} left in place (last round; the analysis drops split seeds){note}")
        return 0
    n = 1
    while (ASIDE / f"round-{n}").exists():
        n += 1
    dest = ASIDE / f"round-{n}"
    dest.mkdir(parents=True)
    moved = [arms[(s, a)][1] for s in rerun for a in ARMS]
    moved += [d for d in sorted(RUNS.glob("h7t-*__*.failed-*"))
              if json.loads((d / "cell.json").read_text(encoding="utf-8")).get("seed") in rerun]
    moved += sorted(RUNS.glob("h7fill-*__*"))
    for d in moved:
        shutil.move(str(d), str(dest / d.name))
    print(f"split seeds {split}, retried seeds {sorted(retried & set(complete))}: set aside {len(moved)} cells "
          f"(with their failed attempts and the fillers) to {dest.relative_to(REPO)}{note}")
    return 4


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
