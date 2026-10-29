#!/usr/bin/env python3
"""Tier B driver (experiments/TIERB.md): runs the KWOK cells one at a time, in the protocol's order, and is safe to
re-invoke (finished cells are skipped; an invalid or failed cell is set aside and rerun once).

    run_tierb.py plan                   write experiments/tierb/plan.json (deterministic) and print it
    run_tierb.py run [--only RUN_ID]    run every pending cell in plan order (or one cell), logging to run.log
    run_tierb.py smoke                  one short mechanics check on a pilot seed, outside the run tree

Paths it relies on (set by the environment the runbook describes): the Tier A binary at $CSIM, the compiled
performance suite at $PERF_TEST, a kind cluster with KWOK and the controller from $IMAGE_REVISION installed.
"""
import argparse, datetime, glob, json, os, random, shutil, subprocess, sys, time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TIERB = os.path.join(REPO, "experiments", "tierb")
RUNS = os.path.join(REPO, "experiments", "runs", "tierb-kwok")
CAL = os.path.join(REPO, "experiments", "runs", "tierb")
LOG = os.path.join(TIERB, "run.log")
SEEDS = range(101, 106)
SIZES = (200, 50)
WARMUP, WINDOW = "5m", "30m"  # WINDOW from the calibration on pilot seeds (RESEARCH_LOG, "Tier B protocol and window")
STOP_AFTER_H = 36.0
MAX_ATTEMPTS = 2
# label -> (NodePool policy, ConsolidationPipeline gate, slug of the tierb-cal run that holds the arm's pipeline)
ARMS = {
    "B2": ("WhenEmptyOrUnderutilized", False, "b2"),
    "B3": ("Balanced", False, "b3"),
    "B4": ("WhenEmptyOrUnderutilized", True, "b4"),
    "heuristic/entropy-greedy": ("WhenEmptyOrUnderutilized", True, "heuristic-entropy-greedy"),
    "heuristic/anneal-plan": ("WhenEmptyOrUnderutilized", True, "heuristic-anneal-plan"),
    "random/anneal-plan": ("WhenEmptyOrUnderutilized", True, "random-anneal-plan"),
    "random/entropy-greedy": ("WhenEmptyOrUnderutilized", True, "random-entropy-greedy"),
}


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    line = f"{now()} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def plan():
    """Rounds by seed; within a round n = 200 then 50; the seven arms shuffled per (seed, n) with a fixed seed."""
    cells = []
    for seed in SEEDS:
        for n in SIZES:
            labels = sorted(ARMS)
            random.Random(f"tierb-{seed}-{n}").shuffle(labels)
            for label in labels:
                cells.append({"run_id": f"tierb-kwok__arm-{ARMS[label][2]}_nodes-{n}_seed-{seed}", "label": label,
                              "seed": seed, "nodes": n, "round": seed})
    return cells


def arm_pipeline(slug):
    """The arm's pipeline config exactly as the simulator ran it (the tierb-cal cell), or None for a gate-off arm."""
    paths = glob.glob(os.path.join(CAL, f"tierb-cal__arm-{slug}_nodes-50_seed-1__*", "cell.json"))
    if len(paths) != 1:
        raise SystemExit(f"expected one tierb-cal cell for arm {slug}, found {len(paths)}")
    return json.load(open(paths[0]))["arm"].get("pipeline")


def workload(seed, n):
    cfg = {"seed": seed, "scenario": {"name": "WB", "nodes": n, "params": {"dropAt": WARMUP}}, "warmup": WARMUP,
           "arm": {"name": "B2-when-empty-or-underutilized"}}
    path = os.path.join(TIERB, f".workload-{seed}-{n}.json")
    json.dump(cfg, open(path, "w"))
    out = subprocess.run([os.environ["CSIM"], "workload", "--config", path], check=True, capture_output=True, text=True)
    os.remove(path)
    return json.loads(out.stdout)


def prices():
    return {c["name"]: c["price_per_hour"] for c in json.load(open(os.path.join(REPO, "experiments", "catalog.json")))}


def run_cell(c, out, window=WINDOW):
    policy, gate, slug = ARMS[c["label"]]
    os.makedirs(out, exist_ok=True)
    w = workload(c["seed"], c["nodes"])
    cell = {**{k: c[k] for k in ("run_id", "label", "seed", "nodes")}, "policy": policy, "gate": gate,
            "pipeline": arm_pipeline(slug) if gate else None, "warmup": WARMUP, "window": window, "workload": w,
            "prices": prices(), "out": out}
    json.dump(cell, open(os.path.join(out, "cell.json"), "w"), indent=2)
    manifest = {"run_id": c["run_id"], "image_revision": os.environ["IMAGE_REVISION"], "image": os.environ["IMAGE"],
                "harness_revision": subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], capture_output=True,
                                                   text=True).stdout.strip(),
                "csim": os.environ["CSIM"], "kind_node_image": os.environ.get("KIND_NODE_IMAGE", ""),
                "started_at": now()}
    env = {**os.environ, "TIERB_CELL": os.path.join(out, "cell.json"), "OUTPUT_DIR": out}
    with open(os.path.join(out, "test.log"), "w") as f:
        rc = subprocess.run([os.environ["PERF_TEST"], "-test.v", "-test.timeout", "2h", "-ginkgo.focus", "Tier B",
                             "-ginkgo.timeout", "2h", "-ginkgo.grace-period", "15m", "-ginkgo.v"],
                            cwd=os.path.join(REPO, "test", "suites", "performance"), env=env, stdout=f,
                            stderr=subprocess.STDOUT).returncode
    manifest.update(finished_at=now(), test_exit_status=rc)
    json.dump(manifest, open(os.path.join(out, "manifest.json"), "w"), indent=2)
    s = os.path.join(out, "summary.json")
    valid = rc == 0 and os.path.exists(s) and json.load(open(s)).get("valid") is True
    return rc, valid


def attempts(out):
    return len(glob.glob(out + ".failed-*"))


def run(only=None):
    cells = plan()
    json.dump(cells, open(os.path.join(TIERB, "plan.json"), "w"), indent=1)
    first = min((json.load(open(m))["started_at"] for m in glob.glob(os.path.join(RUNS, "*", "manifest.json"))),
                default=now())
    start = datetime.datetime.strptime(first, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    for rnd in SEEDS:
        for c in (c for c in cells if c["round"] == rnd and (only is None or c["run_id"] == only)):
            out = os.path.join(RUNS, c["run_id"])
            s = os.path.join(out, "summary.json")
            if os.path.exists(s) and json.load(open(s)).get("valid") is True:
                continue
            if os.path.exists(out):
                if attempts(out) + 1 >= MAX_ATTEMPTS:
                    log(f"ABANDON {c['run_id']} after {attempts(out) + 1} attempts")
                    continue
                shutil.move(out, f"{out}.failed-{attempts(out) + 1}")
            log(f"START {c['run_id']}")
            t = time.time()
            rc, valid = run_cell(c, out)
            log(f"DONE {c['run_id']} rc={rc} valid={valid} in {(time.time() - t) / 60:.1f} min")
        elapsed = (datetime.datetime.now(datetime.timezone.utc) - start).total_seconds() / 3600
        if only is None and elapsed > STOP_AFTER_H:
            log(f"STOP after round {rnd}: {elapsed:.1f} h of Tier B running (stopping rule, {STOP_AFTER_H} h)")
            return
    log("TIER B COMPLETE")


def smoke():
    c = {"run_id": "tierb-smoke__arm-b2_nodes-50_seed-1", "label": "B2", "seed": 1, "nodes": 50, "round": 0}
    out = os.path.join(TIERB, "smoke-out")
    shutil.rmtree(out, ignore_errors=True)
    rc, valid = run_cell(c, out, window="5m")
    print(f"smoke rc={rc} valid={valid} out={out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["plan", "run", "smoke"])
    p.add_argument("--only")
    a = p.parse_args()
    if a.cmd == "plan":
        cells = plan()
        json.dump(cells, open(os.path.join(TIERB, "plan.json"), "w"), indent=1)
        print(json.dumps(cells, indent=1))
    elif a.cmd == "run":
        run(a.only)
    else:
        smoke()
