# Research log

Every decision taken without asking, every fallback, and every blocked host, in order. Times are UTC.

## 2026-10-27

- **Rebase (Phase 0, step 1).** `upstream` (kubernetes-sigs/karpenter) was reachable over git through the session proxy.
  Upstream main head: `32fd95b` (chore: remove dead code and deprecate long-unused exports (#3482)), one commit after the
  `cac0d0b` named in the plan. The fork branch `claude/new-session-03ukbe` had no commits of its own (HEAD `d1aacae` was
  an ancestor of upstream main, 110 commits behind), so the rebase was a fast-forward with no conflicts to resolve.
- **Base check.** `ConsolidationPolicyBalanced` exists at `pkg/apis/v1/nodepool.go:165`; `type Evaluator interface` exists
  at `pkg/controllers/disruption/balanced.go:191`. The base is new enough.
- **Toolchain.** `go version` 1.26.6 matches `go.mod` (`go 1.26.6`). envtest installed exactly as
  `.github/actions/install-deps/action.yaml` does (`go tool -modfile=go.tools.mod setup-envtest use -p path 1.36.x`),
  resolving to Kubernetes **1.36.2**, moved into `/usr/local/kubebuilder/bin`. The primary index worked; no GCS fallback.
- **Python.** The system Python is externally managed, so analysis dependencies live in a venv at `/opt/research-venv`
  (pinned versions in `paper/requirements.txt`).
- **Environment variables.** `BASH_DEFAULT_TIMEOUT_MS` and `BASH_MAX_TIMEOUT_MS` were not set in the session. Long work
  runs as background commands and resumable chunks instead (operating rule 12).
- **Docker.** The daemon was not running; `dockerd` started fine (overlayfs, cgroup v1).
- **Blocked hosts (Tier B).** The network policy denies `registry.k8s.io` and `cgr.dev` (proxy answers 403 to CONNECT).
  Docker Hub (`registry-1.docker.io`) is reachable. KWOK's controller image comes from `registry.k8s.io` and `ko`'s
  default base image from `cgr.dev`, so per the plan's fallback **Tier B runs are skipped**; the KWOK suite is still
  written and must compile. To enable Tier B, add both hosts to the environment's allowed domains (or use Full network
  access) and follow `docs/research/runbook.md`.
- **Baseline (Phase 0, step 3).** On the rebased tree, before any change: `go build ./...` exit 0 in 93 s (cold module
  and build cache); `go test ./pkg/controllers/disruption/... -race` exit 0, package time 399.8 s, wall 524 s including
  compilation. No pre-existing failures.
- **Simulator design spike (Phase 2, Tier A).** `hack/consolidation-sim/spike` builds the same seeded fragmented
  cluster (random c7i/m7i/r7i sizes up to 16 vCPU, each node filled to a random 20-80% of CPU) on two backends and
  times one upstream consolidation cycle. Wall times on the session VM (4 vCPU):

  | backend | nodes | pods | populate | GetCandidatesWithTotals | multi-node ComputeCommands | single-node (stops at first) | one SimulateScheduling |
  | --- | --- | --- | --- | --- | --- | --- | --- |
  | in-memory (fake client) | 100 | 415 | 0.23 s | 4 ms | 92 ms | 6 ms | 5.9 ms |
  | in-memory (fake client) | 1,000 | 3,616 | 2.1 s | 103 ms | 703 ms | 122 ms | 67 ms |
  | envtest | 100 | 415 | 12.4 s | 225 ms | 167 ms | 20 ms | 14 ms |
  | envtest | 1,000 | 3,616 | 105.5 s | 2.2 s | 753 ms | 135 ms | 80 ms |

  **Decision: the in-memory design.** It hosts the same real Karpenter code (state.Cluster, provisioner, disruption
  methods, orchestration queue) on a controller-runtime fake client and a fake clock, and is 20-50x faster on writes,
  which a churn simulation issues constantly. Two fixes made it fast: a plain object tracker (the default field-managed
  tracker rebuilt a REST mapper per write) and a typed pods-by-node index for `spec.nodeName` lists (the fake client
  deep-copies every pod per indexed list). Seconds per simulated cycle are about 0.1 s at 100 nodes and about 1 s at
  1,000 when consolidation finds a move early; a full single-node scan at 1,000 nodes costs about 864 x 67 ms = 58 s,
  which is exactly the upstream cost H7 measures.
- **envtest fidelity trap.** On envtest the same state made different decisions (multi-node: none; single-node:
  replace) because kube-apiserver's TaintNodesByCondition admission adds `node.kubernetes.io/not-ready:NoSchedule` to
  every created Node and no node lifecycle controller removes it, so existing nodes refuse rescheduled pods. Anything
  envtest-based here (the parity harness) must strip that taint, as the disruption suite's `ExpectMakeNodesReady` does.
- **Upstream nondeterminism, observed.** Repeated spike runs with one seed chose multi-node deletes of 34, 35, 23, 21
  and 20 nodes. `cluster.DeepCopyNodes` iterates a map, so candidate order is random, and `sortCandidates` (savings
  ratio, `sort.Slice`) leaves ties, common because nodes of one type with equal pod counts have equal ratios, in that
  random order. The simulator and the parity harness therefore fix candidate order themselves (see the code map).
- **Code map.** A read-only workflow (7 readers, 1 synthesizer, 2 adversarial verifiers) wrote
  `docs/research/code-map.md`; the verifiers checked 1,030 `path:line` citations and corrected about 20. New upstream
  findings it surfaced: only one disruption spec hits a consolidation timeout (single-node, `-5s`), no test checks
  `ConsolidationTimeoutsTotal`, and `newStateFromNodeClaim` drops `podDisruptionCosts`, so Balanced's disruption
  totals for non-candidate nodes depend on the order informer events arrive.
- **Parity design (Phase 1).** Upstream itself is nondeterministic when a state has more than one NodePool:
  `SingleNodeConsolidation.shuffleCandidates` interleaves pools in `lo.Keys(map)` order. Go starts iterating a small
  map at a random one of its 8 slots, so with two pools the second pool comes first only 1 time in 8, and 8 repeated
  upstream runs missed it about a third of the time. Decision: for multi-NodePool states the reference is the set of
  decisions upstream makes over repeated runs, grown adaptively (up to 256 extra upstream runs before a pipeline
  decision counts as a mismatch); single-NodePool states must match exactly, with upstream run twice as a control.
  The simulator's workloads use one NodePool, which removes this source there.
- **Parity, first samples.** 200 states per (policy, catalog) cell: coarse adapters, fine adapters and the
  `balanced` planner all matched upstream on 200/200 in every cell, over a decision mix of multi-node deletes and
  replaces, single-node deletes and replaces, and no-ops (0 to 16 candidates per decision).
- **Mutation check of the parity test.** Three bugs injected one at a time into the fine adapters: dropping the
  multi-node budget decrement was caught (44/60 match); making the upstream planner accept every move was caught on
  Balanced states (85/100) and correctly invisible on WhenEmptyOrUnderutilized states (100/100); skipping the
  `CanPass` pre-filter was not caught, and cannot be, because `CanPass` only skips candidates whose best case (a
  delete) already fails the threshold, so it changes compute, never decisions (an equivalent mutant).
