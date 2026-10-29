#!/usr/bin/env python3
"""Tier B driver (experiments/TIERB.md): runs the KWOK cells one at a time, in the protocol's order, and is safe to
re-invoke. A finished cell is skipped. An invalid one is set aside (.failed-N) and rerun once. One the container
interrupted, or one that never reached the end of its spec, is set aside without counting as an attempt. Two failures
in a row, or an unhealthy cluster, stop the driver.

    run_tierb.py plan                   write experiments/tierb/plan.json (deterministic) and print it
    run_tierb.py run [--only RUN_ID]    run every pending cell in plan order (or one cell), logging to run.log
    run_tierb.py smoke [--label ARM]    one short mechanics check on a pilot seed, outside the run tree

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
# KWOK nodeRegistrationDelay during the warmup and after the drop (TIERB.md, "Node latency")
WARMUP_REGISTRATION_DELAY, REGISTRATION_DELAY = "0s", "47s"
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


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def cell_valid(out):
    """A cell counts only if its spec exited 0 and its summary says valid (the spec's own checks)."""
    m, s = load(os.path.join(out, "manifest.json")), load(os.path.join(out, "summary.json"))
    return bool(m and m.get("test_exit_status") == 0 and s and s.get("valid") is True)


def require_env():
    for k in ("CSIM", "PERF_TEST", "IMAGE_REVISION", "IMAGE"):
        if not os.environ.get(k):
            raise SystemExit(f"{k} is not set")
    for k in ("CSIM", "PERF_TEST"):
        if not os.access(os.environ[k], os.X_OK):
            raise SystemExit(f"{k}={os.environ[k]} is not an executable file")


def preflight():
    """The cluster must be healthy before a cell starts; a failure stops the driver without touching any cell."""
    def k(*args):
        return subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=180).returncode == 0
    ok = k("get", "--raw", "/readyz") and all(
        k("-n", "kube-system", "wait", "--for=condition=Available", "--timeout=120s", f"deploy/{d}")
        for d in ("karpenter", "kwok-controller-a", "coredns"))
    if not ok:
        log("PREFLIGHT FAILED: the cluster is not healthy; stopping without touching any cell")
        sys.exit(2)


def run_cell(c, out, window=WINDOW):
    policy, gate, slug = ARMS[c["label"]]
    pipe = arm_pipeline(slug) if gate else None
    if pipe is not None and not pipe.get("seed"):
        pipe["seed"] = c["seed"]  # as the simulator does (sim/world.go: a pipeline seed of 0 becomes the run seed)
    cell = {**{k: c[k] for k in ("run_id", "label", "seed", "nodes")}, "policy": policy, "gate": gate,
            "pipeline": pipe, "warmup": WARMUP, "window": window,
            "warmup_registration_delay": WARMUP_REGISTRATION_DELAY, "registration_delay": REGISTRATION_DELAY, "workload": workload(c["seed"], c["nodes"]),
            "prices": prices(), "out": out}
    manifest = {"run_id": c["run_id"], "image_revision": os.environ["IMAGE_REVISION"], "image": os.environ["IMAGE"],
                "harness_revision": subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], capture_output=True,
                                                   text=True).stdout.strip(),
                "harness_dirty": subprocess.run(["git", "-C", REPO, "status", "--porcelain", "--", "test", "experiments/tierb"],
                                                capture_output=True, text=True).stdout.strip() != "",
                "perf_test": os.environ["PERF_TEST"], "csim": os.environ["CSIM"],
                "kind_node_image": os.environ.get("KIND_NODE_IMAGE", ""),
                "boot_id": open("/proc/sys/kernel/random/boot_id").read().strip(),
                "status": "running", "started_at": now()}
    os.makedirs(out, exist_ok=True)
    write_json(os.path.join(out, "cell.json"), cell)
    write_json(os.path.join(out, "manifest.json"), manifest)
    env = {**os.environ, "TIERB_CELL": os.path.join(out, "cell.json"), "OUTPUT_DIR": out}
    log_path = os.path.join(out, "test.log")
    with open(log_path, "w") as f:
        rc = subprocess.run([os.environ["PERF_TEST"], "-test.v", "-test.timeout", "2h", "-ginkgo.focus", "Tier B",
                             "-ginkgo.timeout", "2h", "-ginkgo.grace-period", "15m", "-ginkgo.v"],
                            cwd=os.path.join(REPO, "test", "suites", "performance"), env=env, stdout=f,
                            stderr=subprocess.STDOUT).returncode
    subprocess.run(["gzip", "-9f", log_path], check=False)
    manifest.update(finished_at=now(), test_exit_status=rc, status="done")
    manifest["valid"] = rc == 0 and (load(os.path.join(out, "summary.json")) or {}).get("valid") is True
    write_json(os.path.join(out, "manifest.json"), manifest)
    return rc, manifest["valid"]


def attempts(out):
    return len(glob.glob(out + ".failed-*"))


def round_started(cells):
    return any(os.path.exists(os.path.join(RUNS, c["run_id"])) or glob.glob(os.path.join(RUNS, c["run_id"]) + ".*")
               for c in cells)


def run(only=None):
    require_env()
    cells = plan()
    write_json(os.path.join(TIERB, "plan.json"), cells)
    firsts = [m["started_at"] for m in (load(p) for p in glob.glob(os.path.join(RUNS, "*", "manifest.json"))
                                        + glob.glob(os.path.join(RUNS, "*.*", "manifest.json"))) if m]
    start = datetime.datetime.strptime(min(firsts, default=now()), "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)
    failures = 0
    for rnd in SEEDS:
        rcells = [c for c in cells if c["round"] == rnd and (only is None or c["run_id"] == only)]
        elapsed = (datetime.datetime.now(datetime.timezone.utc) - start).total_seconds() / 3600
        # Stopping rule (time-only): no new round starts after STOP_AFTER_H; a round already started is finished.
        if only is None and elapsed > STOP_AFTER_H and not round_started(rcells):
            log(f"STOP before round {rnd}: {elapsed:.1f} h of Tier B running (stopping rule, {STOP_AFTER_H} h)")
            return
        for c in rcells:
            out = os.path.join(RUNS, c["run_id"])
            while not cell_valid(out):
                m = load(os.path.join(out, "manifest.json"))
                if os.path.exists(out) and (m is None or m.get("status") != "done"):
                    # interrupted mid-cell (container restart): not an attempt, the cell is simply run again
                    shutil.move(out, f"{out}.interrupted-{int(time.time())}")
                elif os.path.exists(out):
                    if attempts(out) + 1 >= MAX_ATTEMPTS:
                        log(f"ABANDON {c['run_id']} after {attempts(out) + 1} attempts")
                        break
                    shutil.move(out, f"{out}.failed-{attempts(out) + 1}")
                preflight()
                log(f"START {c['run_id']} (attempt {attempts(out) + 1})")
                t = time.time()
                rc, valid = run_cell(c, out)
                log(f"DONE {c['run_id']} rc={rc} valid={valid} in {(time.time() - t) / 60:.1f} min")
                finished = os.path.exists(os.path.join(out, "summary.json"))
                if not finished:
                    # the spec never reached its end (setup or infrastructure): not an attempt
                    shutil.move(out, f"{out}.infra-{int(time.time())}")
                failures = 0 if rc == 0 and finished else failures + 1
                if failures >= 2:
                    log("STOP: two cells in a row failed; check the cluster before re-invoking")
                    sys.exit(3)
    log("TIER B COMPLETE" if only is None else f"DONE --only {only}")


def smoke(label, nodes):
    require_env()
    preflight()
    c = {"run_id": f"tierb-smoke__arm-{ARMS[label][2]}_nodes-{nodes}_seed-1", "label": label, "seed": 1,
         "nodes": nodes, "round": 0}
    out = os.path.join(TIERB, f"smoke-{ARMS[label][2]}-{nodes}")
    shutil.rmtree(out, ignore_errors=True)
    rc, valid = run_cell(c, out, window="5m")
    print(f"smoke rc={rc} valid={valid} out={out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["plan", "run", "smoke"])
    p.add_argument("--only")
    p.add_argument("--label", default="B2", help="smoke: the arm to check")
    p.add_argument("--nodes", type=int, default=50, help="smoke: the size")
    a = p.parse_args()
    if a.cmd == "plan":
        cells = plan()
        json.dump(cells, open(os.path.join(TIERB, "plan.json"), "w"), indent=1)
        print(json.dumps(cells, indent=1))
    elif a.cmd == "run":
        run(a.only)
    else:
        smoke(a.label, a.nodes)
