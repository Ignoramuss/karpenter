# Related work (candidate references)

Every entry is marked **verify**: titles, venues and years are from memory and must be checked against the source
before citation. Notes say what each reference is for in this paper.

## In this repository

- **Balanced consolidation RFC**, `designs/balanced-consolidation.md` (kubernetes-sigs/karpenter). Scores a
  node-deleting move by savings fraction over disruption fraction and approves it at or above 1/k, k = 2
  (`consolidationPolicy: Balanced`). Its open questions list asynchronous move generation (generate, score, enqueue,
  execute as budget allows), which the pipeline implements. Balanced's acceptance is our planning energy with
  alpha = 0, lambda = 1, mu = k. Its FAQ recommends MostAllocated scoring because kube-scheduler can scatter evicted
  pods; Exp 5 measures that. **verify** (cite by path and commit).
- **Pod deletion cost controller RFC**, `designs/pod-deletion-cost-controller.md` (gate `PodDeletionCostManagement`).
  Argues that ReplicaSet scale-down spreading raises cluster entropy and steers scale-down toward consolidation
  targets: a zero-disruption entropy lever. Our `deletion-cost` scale-down model uses its ranking
  (`pkg/controllers/deletioncost`). **verify**.

## Kubernetes ecosystem

- **Kubernetes descheduler**, `HighNodeUtilization` strategy (kubernetes-sigs/descheduler): evicts pods from
  underutilized nodes so a MostAllocated scheduler compacts them. Baseline B5. **verify** (cite the project docs and
  version).
- **kube-scheduler NodeResourcesFit scoring strategies** (`LeastAllocated`, `MostAllocated`,
  `RequestedToCapacityRatio`), Kubernetes documentation, "Resource Bin Packing". Placement models in the simulator
  and Tier B's scheduler profile. **verify**.
- **Cluster Autoscaler scale-down** (kubernetes/autoscaler): utilization-threshold node removal; a contrast to
  Karpenter's simulation-based consolidation. **verify**.

## Packing and scheduling

- **Tetris**: R. Grandl, G. Ananthanarayanan, S. Kandula, S. Rao, A. Akella. "Multi-resource packing for cluster
  schedulers." SIGCOMM 2014. Alignment-score packing across dimensions; motivates the multi-resource potentials and
  the stranding term. **verify**.
- **Dominant Resource Fairness**: A. Ghodsi, M. Zaharia, B. Hindman, A. Konwinski, S. Shenker, I. Stoica.
  "Dominant resource fairness: fair allocation of multiple resource types." NSDI 2011. Source of the dominant-share
  summary of utilization. **verify**.
- **Borg**: A. Verma, L. Pedrosa, M. Korupolu, D. Oppenheimer, E. Tune, J. Wilkes. "Large-scale cluster management
  at Google with Borg." EuroSys 2015. Scoring (stranded resources, best fit vs worst fit) and the cost of packing
  tightly. **verify**.
- **Protean**: O. Hadary et al. "Protean: VM allocation service at scale." OSDI 2020. Rule-based VM placement and
  packing at cloud scale. **verify**.
- **Google cluster trace**: J. Wilkes. "More Google cluster data." Google research blog, 2011; and C. Reiss,
  J. Wilkes, J. Hellerstein, "Google cluster-usage traces: format + schema," 2011. W6's source; license and mapping in
  `hack/consolidation-sim/traces/README.md`. **verify**.
- **Vector bin packing**: surveys of multi-dimensional bin packing heuristics and lower bounds (for example
  Panigrahy, Talwar, Uyeda, Wieder, "Heuristics for vector bin packing," Microsoft Research technical report, 2011).
  Context for the B6 bound and the first-fit-decreasing upper bound. **verify**.

## Optimization method

- **Simulated annealing**: S. Kirkpatrick, C. D. Gelatt, M. P. Vecchi. "Optimization by simulated annealing."
  Science 220(4598), 1983. Metropolis acceptance and geometric cooling in anneal-plan and anneal-online. **verify**.
- **Metropolis-Hastings**: N. Metropolis, A. Rosenbluth, M. Rosenbluth, A. Teller, E. Teller. "Equation of state
  calculations by fast computing machines." J. Chem. Phys. 21, 1953. **verify**.
- **Concave minimization over polytopes attains a vertex**: standard result (for example Rockafellar, *Convex
  Analysis*, 1970, Corollary 32.3.2, or Horst and Tuy, *Global Optimization: Deterministic Approaches*). Underpins
  the packed-or-empty argument in the formal model. **verify** the exact statement and citation.
- **Two one-sided tests**: D. J. Schuirmann. "A comparison of the two one-sided tests procedure and the power
  approach for assessing the equivalence of average bioavailability." J. Pharmacokinet. Biopharm. 15, 1987. H1's
  equivalence test. **verify**.
- **Holm correction**: S. Holm. "A simple sequentially rejective multiple test procedure." Scand. J. Statist. 6,
  1979. **verify**.
- **CP-SAT**: L. Perron and F. Didier, "CP-SAT," Google OR-Tools (software, version pinned in
  `paper/requirements.txt`). Used for the B6 bound. **verify** the preferred citation.
