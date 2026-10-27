# Consolidation code map

This map describes how Karpenter decides on and carries out consolidation today. It is written for engineers who are
building the pluggable consolidation pipeline in `pkg/controllers/disruption/pipeline`. That pipeline has four stages:

1. CandidateSelector
2. MoveGenerator
3. Planner, which applies budgets
4. Executor

The stages share a Snapshot, a Potential, a Feasibility check and a Recorder. The new method, `PipelineConsolidation`,
sits behind the alpha gate `ConsolidationPipeline`, which is off by default. Its upstream adapters must reproduce
current decisions exactly, and a differential parity test proves that they do:

| Stage | Upstream adapters |
| --- | --- |
| Candidates | `upstream` |
| Moves | `upstream-single`, `upstream-multi` |
| Planner | `upstream`, `balanced` |
| Executor | `upstream-queue` |

**Conventions**

- All line numbers refer to upstream `kubernetes-sigs/karpenter` main at `32fd95b`, and each `path:line` below was
  checked against that tree. Line ranges are inclusive.
- Path abbreviations:
  - `disruption/` is `pkg/controllers/disruption/`
  - `state/` is `pkg/controllers/state/`
  - `scheduling/` is `pkg/controllers/provisioning/scheduling/`
  - `provisioner.go` is `pkg/controllers/provisioning/provisioner.go`
  - `cloudprovider/` is `pkg/cloudprovider/`
  - Every other path is relative to the repository root.
- "Injected clock" means the `clock.Clock` passed in at construction. Production passes one clock to everything
  (`pkg/controllers/controllers.go:122-129`); the unit suites pass `env.Clock`, a fake clock. "Wall clock" means a
  direct call to `time.*` or to `context.WithTimeout`.

**Baseline on the session VM** (see `experiments/RESEARCH_LOG.md`)

- `go build ./...` takes 93 s with a cold cache.
- `go test ./pkg/controllers/disruption/... -race` passes; the package itself takes 399.8 s.
- The same package without `-race` passes in 302 s.

---

## 1. One disruption cycle, end to end

The disruption controller is a singleton reconciler. One pass of `Reconcile` tries each registered method in order
and stops at the first method that starts a command. The trace below follows a pass where Repair is off, Emptiness,
StaticDrift and Drift find nothing, multi-node consolidation finds nothing, and single-node consolidation replaces a
node.

### 1.1 The trace

**Wiring and loop entry (steps 1-5)**

1. **Wiring.** `pkg/controllers/controllers.go:122` builds the provisioner and `:124` the disruption queue
   (`disruption.NewQueue`). `:126` builds the `cost.ClusterCost`. `:129` calls `disruption.NewController(...)` with
   **no options**, so the default method list from `NewMethods` is used (`disruption/controller.go:87`, `:102-123`).
   `Register` makes the controller a singleton (`disruption/controller.go:129-134`).
2. **Entry bookkeeping.** `Reconcile` (`disruption/controller.go:137`) does three things first:
   - It calls `logAbnormalRuns` at entry and again through `defer`. That function logs at V(1) when a recorded run is
     more than `AbnormalTimeLimit = 15 * time.Minute` old (`:141-142`, `:259-268`).
   - It calls `recordRun("disruption-loop")` (`:143`).
   - It calls `logInvalidBudgets` (`:147`, `:271-290`). For each managed NodePool and each method's `Reason()`, this
     calls `GetAllowedDisruptionsByReason(c.clock, 100, reason)` and logs any error.
3. **Sync gate.** If `!c.cluster.Synced(ctx)`, `Reconcile` returns `RequeueAfter: time.Second` (`:152-154`).
   - `Synced` is at `state/cluster.go:152`. After the first successful sync it only checks that no tracked NodeClaim
     has an empty providerID (`:185-197`).
   - Before that, it lists NodeClaims and Nodes and requires state to hold a superset of both (`:203-246`).
4. **Taint cleanup.** The controller takes `outdatedNodes`: every node in `DeepCopyNodes()` that is not in the queue
   (`HasAny`), not `MarkedForDeletion()` and not `RebootInProgress()` (`disruption/controller.go:160-162`). For those
   nodes:
   - `state.RequireNoScheduleTaint(..., false, ...)` removes `karpenter.sh/disrupted:NoSchedule` (`:163`).
   - `state.ClearNodeClaimsCondition(..., v1.ConditionTypeDisruptionReason, ...)` clears the condition (`:169`).
   - A conflict returns `Requeue: true` (`:164-166`, `:170-172`).
   - This is also the rollback for a `StartCommand` that failed part-way (section 1.2).
5. **Prediction gate.** With `PredictionEnabled` on and the prediction store not hydrated, the controller returns
   `RequeueAfter: time.Second` (`:175-177`).

**The method loop (steps 6-12)**

6. **Method loop** (`:180-192`). For each method `m` in `c.methods`, in order:
   - Call `recordRun(fmt.Sprintf("%T", m))`, then `c.disrupt(ctx, m)`.
   - An error stops the pass. A conflict returns `Requeue: true`; any other error is wrapped and returned.
   - The **first method that returns `true` wins**. It returns `RequeueAfter: singleton.RequeueImmediately`, which is
     1 ns (`github.com/awslabs/operatorpkg/singleton/controller.go:18`). Later methods do not run this pass.
   - If no method acts, the controller returns `RequeueAfter: pollingPeriod`, where `pollingPeriod = 10 * time.Second`
     (`:72`, `:195`).
7. **`disrupt(ctx, m)`** (`disruption/controller.go:198`). The deferred `EvaluationDurationSeconds{reason,
   consolidation_type}` timer captures its labels when the `defer` runs, before any work is done (`:199-202`).
8. **Candidates.** `disrupt` calls
   `GetCandidatesWithTotals(ctx, c.cluster, c.kubeClient, c.recorder, c.clock, c.cloudProvider, disruption.ShouldDisrupt, disruption.Class(), c.queue, c.clusterCost)`
   (`:203`, defined at `disruption/helpers.go:273-299`). Candidates are **rebuilt for every method**. The function:
   1. Calls `BuildNodePoolMap` (`helpers.go:276`, `:311-342`), which lists managed NodePools and calls
      `cloudProvider.GetInstanceTypes` for each.
   2. Calls `pdb.NewLimits` (`:280`).
   3. Freezes `fixedNowClock{Clock: clk, now: clk.Now()}` (`:288`, `:302-308`).
   4. Calls `cluster.DeepCopyNodes()` (`:289`).
   5. Runs `NewCandidate` per node, keeping only nodes that return no error (`:290-293`; `disruption/types.go:151`).
   6. Runs `computeNodePoolTotals` over **all** candidates, before filtering (`:296`; `disruption/balanced.go:48`).
   7. Filters with the method's `ShouldDisrupt` (`:297`).

   Back in `disrupt`, the `EligibleNodes{reason}` gauge is set (`controller.go:207`). If no candidates remain,
   `disrupt` returns `false` (`:212-214`).
9. **Totals.** If the method implements `NodePoolTotalsSetter`, `disrupt` calls `SetNodePoolTotals(nodePoolTotals)`
   (`controller.go:216-218`). The consolidation implementation replaces the method's evaluator with
   `NewBalancedEvaluator(totals, c.recorder)` (`disruption/consolidation.go:71-73`).
10. **Budgets.** `BuildDisruptionBudgetMapping(ctx, c.cluster, c.clock, c.kubeClient, c.cloudProvider, c.recorder, disruption.Reason())`
    (`controller.go:219`; `helpers.go:417-438`) returns NodePool name -> remaining allowed disruptions for this reason.
    Section 3.7 covers it.
11. **Compute.** `disrupt` calls `cmds, err := disruption.ComputeCommands(ctx, disruptionBudgetMapping, candidates...)`
    (`controller.go:224`).
    - For `MultiNodeConsolidation` (`disruption/multinodeconsolidation.go:50`), the call returns early because
      `IsConsolidated()` is true, or it runs the prefix binary search and finds nothing. `disrupt` returns `false`.
    - The loop then calls `disrupt()` again for `SingleNodeConsolidation`, repeating steps 7-10 with fresh
      candidates, totals and budgets. Its `ComputeCommands` (`disruption/singlenodeconsolidation.go:53`) walks the
      sorted candidates (section 3.4).
12. **Simulate.** Each attempt calls `computeConsolidation` (`disruption/consolidation.go:159`), which calls
    `SimulateScheduling(ctx, kubeClient, cluster, provisioner, clock, recorder, []pscheduling.Options{pscheduling.IsConsolidationSimulation}, SimulationOptions{}, candidates...)`
    (`consolidation.go:162`; `helpers.go:64-168`). That in turn calls:
    - `provisioner.GetPendingPods` (`helpers.go:83`; `provisioner.go:213-242`)
    - `provisioner.NewScheduler` (`helpers.go:128-134`; `provisioner.go:282-378`)
    - `Scheduler.Solve` (`helpers.go:139`; `scheduling/scheduler.go:479-558`)
    - `Results.TruncateInstanceTypes(ctx, MaxInstanceTypes=600)` (`helpers.go:143`; `scheduling/scheduler.go:458-476`;
      `scheduling/nodeclaimtemplate.go:50`)

    `computeConsolidation` then classifies the result as delete or replace and applies the price and spot-to-spot
    filters (`consolidation.go:172-253`).

**Scoring, validation and hand-off (steps 13-16)**

13. **Evaluator.** Single-node consolidation runs `CanPassThreshold` before the simulation and `ApproveCommand`
    after it (`singlenodeconsolidation.go:86-88`, `:100-102`). Both are implemented at `disruption/balanced.go:288-300`
    and `:221-251`. Section 4 covers them.
14. **Validation after the wait.** `s.validator.Validate(ctx, cmd, commandValidationDelay)` (`singlenodeconsolidation.go:103`).
    - It blocks on `clock.After(15s)` (`disruption/consolidation.go:45`; `disruption/validation.go:200-206`).
    - It then re-derives candidates and fails with `ChurnValidationError` if **any** candidate is no longer valid
      (`validation.go:263-274`).
    - It re-checks nomination and budgets (`:275-292`), then re-simulates and checks the replacement
      (`validation.go:297-356`).
    - It re-checks candidates once more (`:214-218`).
    - On a `ValidationError`, single-node consolidation emits `ConsolidationRejected` events and returns no command
      (`singlenodeconsolidation.go:104-107`). Section 3.8 covers validation.
15. **Hand-off** (`controller.go:228-250`).
    - No-op commands are dropped (`:228`).
    - For each command, inside `workqueue.ParallelizeUntil` (`:234`), `disrupt` stamps `CreationTimestamp = c.clock.Now()`,
      `ID = uuid.New()` and `Method = disruption` (`:238-240`).
    - It then calls `c.queue.StartCommand(ctx, &cmd)` (`:243`; `disruption/queue.go:365-451`). Section 1.2 covers
      `StartCommand`.
    - Any error fails the pass (`:247-249`).
16. **Requeue.** `disrupt` returns `true`, and `Reconcile` returns `RequeueImmediately` (`controller.go:189-191`).

**Execution, termination and state sync (steps 17-19)**

17. **Queue execution.** The queue controller reconciles the first candidate's NodeClaim (`queue.go:143`), waits for
    replacements to initialize, then deletes the candidate NodeClaims (`waitOrTerminate`, `queue.go:187-268`). The
    queue does not wait for drain.
18. **Termination and drain.** The NodeClaim and Node finalizers taint, drain, wait for volume detachment, terminate
    the instance and remove the finalizers (section 1.3).
19. **State sync.** Informers feed `state.Cluster`. Deleting NodeClaims, Nodes and pods, and binding pods on the
    replacement, all call `MarkUnconsolidated()` (section 1.4). The next pass sees `IsConsolidated() == false` and
    evaluates consolidation again.

Two timing consequences follow from this shape:

- **The queue's timeout starts after validation.** `CreationTimestamp` is set after validation, so the queue's retry
  clock starts after the 15 s wait.
- **Waits can stack in one pass.** When a method's validation fails, its `ComputeCommands` returns `[]Command{}, nil`
  and `disrupt` returns `false`. The loop moves on to the next method in the same pass, so two failed validations
  (Emptiness then Multi, or Multi then Single) stack 15 s waits.

### 1.2 Orchestration queue

There is no `pkg/controllers/disruption/orchestration/` package. Commands go straight to `*Queue`
(`disruption/queue.go`).

**State and constants**

- `type Queue struct` holds `sync.RWMutex`, `ProviderIDToCommand map[string]*Command`, `source chan
  event.TypedGenericEvent[*v1.NodeClaim]`, `kubeClient`, `recorder`, `cluster`, `clock` and `provisioner`
  (`queue.go:96-105`). The channel buffer is 10000 (`:114`).
- `HasAny(ids ...string) bool` reads under the RLock (`:480-491`). This is what makes `NewCandidate` reject nodes that
  are already in flight (`types.go:155-157`).
- Constants (`queue.go:63-70`):

  | Constant | Value |
  | --- | --- |
  | `queueBaseDelay` | 1s |
  | `queueMaxDelay` | 10s |
  | `minRetryDuration` | 10m |
  | `maxRetryDuration` | 1h |
  | `maxConcurrentReconciles` | 100 (declared, unused) |
  | `retryDurationScale` | 80ms |

- `GetMaxRetryDuration` is `clamp(80ms x len(ProviderIDToCommand), 10m, 1h)` (`:88-94`). The map counts candidates,
  not commands.
- The rate limiter is max(exponential 1s to 10s, bucket of 100 qps with a burst of 1000). Concurrency is
  `LinearScaleReconciles(CPU, 100, 1000)` (`:133-139`).

**`func (q *Queue) StartCommand(ctx context.Context, cmd *Command) error`** (`queue.go:365-451`) runs these steps in
order:

1. A deferred function releases static NodePool reservations (`NodePoolState.ReleaseNodeCount`) unless ownership has
   passed to the provisioner (`:366-379`).
2. `HasAny(providerIDs...)` returns "candidate is being disrupted" (`:384-386`). The function logs "disrupting
   node(s)" with `cmd.LogValues()` (`:388-390`).
3. A `RebootDecision` only records metrics and returns (`:393-399`).
4. `markDisrupted` (`:309-343`) runs in parallel per candidate:
   - `state.RequireNoScheduleTaint(ctx, kubeClient, true, stateNode)` adds `karpenter.sh/disrupted:NoSchedule`
     (`:312`; `state/statenode.go:611-661`; `pkg/apis/v1/taints.go:27,35-38`).
   - A status patch sets `DisruptionReason=True` with reason `cmd.Reason()`, using `status.WithClock(q.clock)`
     (`:323-324`).
   - Static-pool candidates are also marked pending-disruption (`:338-340`).
5. If marking failed **and** (there are replacements **or** nothing was marked), return the error (`:405-407`).
   Otherwise narrow `cmd.Candidates` to the marked subset (`:410`). A delete can therefore go ahead with a subset.
6. `createReplacementNodeClaims` (`:346-359`) calls `provisioner.CreateNodeClaims(ctx, nodeClaims,
   provisioning.WithReason(lower(reason)))`.
   - `CreateNodeClaims` is at `provisioner.go:190` and calls `Create` (`:490-543`) in parallel.
   - The created names are written to `cmd.Replacements[i].Name`. A count mismatch is an error. A failure returns
     "launching replacement nodeclaim" (`:414-418`).
7. `cluster.MarkForDeletion(providerIDs...)` (`:425`; `state/cluster.go:367-381`). This must come **after** the launch,
   so the provisioner does not launch capacity for the same pods twice (`:419-424`).
8. `cmd.Results.Record(NopLogger ctx, recorder, cluster)` (`:436`; `scheduling/scheduler.go:330-360`) nominates every
   existing node that received a real pod in the **original** simulation. The nomination window is
   `max(2*BatchMaxDuration, 10s)` (`state/statenode.go:602-605`). Emptiness commands carry zero `Results`, so they
   nominate nothing.
9. Under the lock, map every candidate's providerID to `cmd` and push **only `cmd.Candidates[0].NodeClaim`** onto the
   channel (`:438-446`).
10. `recordDecisionPerformed` (`:449`, `:453-477`) increments `NodepoolDecisionsPerformed`,
    `TerminateFirstDecisionsTotal` (when the reason is set) and `DecisionsPerformedTotal`.

**`Reconcile` and `waitOrTerminate`** (`queue.go:143-268`)

- `reconcile.AsReconciler` first gets the NodeClaim and ignores NotFound. If `Candidates[0]` disappears before the queue
  reconciles it, the command stays in the map and is never reconciled again.
- The command is looked up by `nodeClaim.Status.ProviderID` (`:146`).
- A deferred check turns any error into `UnrecoverableError("command reached timeout")` once
  `q.clock.Since(cmd.CreationTimestamp) > retryDuration` (`:192-196`).
- For each replacement that is not yet initialized (`:198-225`):
  - If the Get returns NotFound **and** `!cluster.NodeClaimExists(name)`, the error is unrecoverable: "replacement was
    deleted". Any other Get error records a "getting node claim" wait error and skips that replacement (`:209-213`).
  - When the Get succeeds, the queue publishes `Launching`. If `Initialized` is not true, it publishes
    `WaitingOnReadiness` and records a wait error. If it is true, it sets `Replacement.Initialized`.
  - Any wait error returns "waiting for replacement initialization", which leads to `RequeueAfter: 1s` (`:227-229`,
    `:156-158`).
- When every replacement is initialized (or there are none), the queue runs per candidate in parallel (`:234-264`):
  1. Optionally `TightenTerminationTimestampAnnotation(now + candidate.TerminationGracePeriod)`. Only Repair sets
     this.
  2. `kubeClient.Delete(NodeClaim)` with `retry.DefaultBackoff`, ignoring NotFound.
  3. The `Terminating` event.
  4. `recordCandidateDisrupted` (`:272-305`), which increments `NodeClaimsDisruptedTotal` and
     `PodsDisruptionInitiatedTotal` (+`len(reschedulablePods)`).

  "Success" means the API server accepted the deletes; the queue does not wait for drain.
- `CompleteCommand` (`:504-514`) calls `UnmarkForDeletion` when `!Succeeded`, then removes the map entries.

**Failure and rollback**

| Failure point | What happens |
| --- | --- |
| Unrecoverable in `Reconcile`: a replacement was deleted, or the command timed out (`queue.go:159-175`) | `DisruptionQueueFailuresTotal` += the number of uninitialized replacements. The taint is removed and the `DisruptionReason` condition cleared. Then `CompleteCommand` unmarks deletion (`state/cluster.go:350-364`). Replacement NodeClaims that were already launched are **not** deleted. |
| Success (`queue.go:177-178`) | `cmd.Succeeded = true`. `CompleteCommand` removes the map entries and the nodes stay marked for deletion. |
| `StartCommand` fails after `markDisrupted` | There is no in-line rollback. The next `Reconcile` removes the taint and the condition from every node that is not queued, not marked for deletion and not rebooting (`controller.go:160-174`). |

### 1.3 Termination and drain

**NodeClaim finalizer** (`pkg/controllers/nodeclaim/lifecycle/controller.go:198-287`)

1. `ensureTerminationGracePeriodTerminationTimeAnnotation` (`:289-309`) runs first. When
   `Spec.TerminationGracePeriod != nil`, it stamps `karpenter.sh/nodeclaim-termination-timestamp` as DeletionTimestamp
   + TGP, unless the annotation is already present (`pkg/apis/v1/labels.go:59`).
2. If the NodeClaim is registered, delete its Nodes, and return while any remain (`:213-231`).
3. Call `cloudProvider.Delete`, set `InstanceTerminating=True`, and requeue every 5 s until the instance is NotFound
   (`:233-261`).
4. Remove the finalizer and record `NodeClaimTerminationDurationSeconds` and `NodeClaimsTerminatedTotal`
   (`:262-283`).

**Node finalizer.** `finalize` (`pkg/controllers/node/termination/controller.go:93-191`) runs these steps:

1. Delete the NodeClaim if it is not already deleting (`:108-115`).
2. If the Node is not Ready and the cloud provider reports NotFound, remove the finalizer right away (`:121-128`).
3. Read `nodeTerminationTime` from the annotation (`:392-406`). It is nil when there is no TGP.
4. `terminator.Taint(ctx, node, v1.DisruptedNoScheduleTaint)` (`:134`). This also labels the node
   `node.kubernetes.io/exclude-from-external-load-balancers=karpenter` (`terminator/terminator.go:53-90`).
5. Run `awaitDrain`, then `awaitVolumeDetachment`, then `awaitInstanceTermination`. The first step that returns a
   result or an error stops the chain (`:147-156`).
   - **`awaitDrain`** (`:198-229`):
     - Sets `Drained=Unknown`, then calls `terminator.Drain(..., terminator.ForceDelete)`.
     - A `NodeDrainError` publishes `NodeFailedToDrain` and requeues after 1 s.
     - After the drain, `Drained` stays Unknown until `MinDrainTime = 5s` (`:62`) has passed since its
       LastTransitionTime.
   - **`awaitVolumeDetachment`** (`:236-277`) waits on VolumeAttachments, excluding those of non-drainable pods
     (`:324-359`). Once the TGP has elapsed (`:302-307`), it gives up.
   - **`awaitInstanceTermination`** (`:282-300`) calls `cloudProvider.Delete` and requeues every 5 s.
6. `removeFinalizer` (`:361-390`) records `NodesTerminatedTotal`, `DurationSeconds` and `NodeLifetimeDurationSeconds`.
   The metrics use wall-clock `time.Since` (`:379`, `:383`).
7. `time.After(time.Second)` at `:179` is a **no-op**: the returned channel is never received.

**`Drain`.** The signature is
`func (t *Terminator) Drain(ctx context.Context, node *corev1.Node, nodeGracePeriodExpirationTime *time.Time, mode DrainMode) error`
(`terminator/terminator.go:104`).

1. List the node's pods and keep those that are `IsWaitingEviction`: not terminal, not tolerating the disrupted taint,
   not stuck terminating, and not mirror pods (`terminator.go:110`; `pkg/utils/pod/scheduling.go:69-85,155-160`).
   **do-not-disrupt pods are included, so a drain stalls on them** (`scheduling.go:78`).
2. Pods past the force-delete threshold (`needsForceDelete`, `terminator/eviction.go:257-269`) are queued across all
   tiers at once (`terminator.go:123-133`).
3. The remaining pods are grouped into four tiers (`groupPodsByPriority`, `:153-172`):
   1. non-critical, non-DaemonSet
   2. non-critical DaemonSet
   3. critical, non-DaemonSet
   4. critical DaemonSet

   "Critical" means the PriorityClassName is `system-cluster-critical` or `system-node-critical`. Only the **first
   non-empty tier** is queued per call, and the call returns a `NodeDrainError` (`:134-144`).

**Eviction queue** (`terminator/eviction.go`)

- Items are keyed by `{NamespacedName, UID}`. A merge keeps the earlier deadline, and force-delete beats timeout
  (`:151-205`). The backoff runs from 100 ms to 10 s (`:58-59`).
- `Reconcile` (`:215-249`) checks in order: unknown UID, expired timeout, `needsForceDelete`, inactive pod, and
  `!IsEvictable`. `IsEvictable` (`scheduling.go:59-64`) checks do-not-disrupt, including duration values
  (`scheduling.go:238-280`). A pod that passes all of these is evicted.
- `evict` (`:272-325`) uses the eviction subresource with a UID precondition, so it **respects PDBs**:
  - 404 or 409 means done.
  - 429 means a `NodeFailedToDrain` event and a requeue.
  - 200 means an `EvictPod` event and an increment of `PodsDrainedTotal`.
- `forceDelete` (`:331-359`) deletes with `GracePeriodSeconds = max(deadline-now, 1)`, bypassing PDBs and do-not-disrupt.
- Without a TGP the deadline is nil and force-delete never happens. A do-not-disrupt pod or a fully blocking PDB then
  holds the node indefinitely.

### 1.4 State sync and the consolidation timestamp

**Informers** (`state/informer/`)

| Controller | Effect |
| --- | --- |
| Pod (`pod.go:60`) | NotFound calls `DeletePod`; otherwise `UpdatePod`. Requeues every 1m. |
| Node (`node.go:53`) | `UpdateNode` or `DeleteNode`. |
| NodeClaim (`nodeclaim.go:61`) | `UpdateNodeClaim` or `DeleteNodeClaim`, plus ClusterCost updates. |
| NodePool (`nodepool.go:61`) | `MarkUnconsolidated()` (`:78`), on generation changes. On NotFound it only removes the pool from ClusterCost (`:68`) and does not mark unconsolidated. |
| DaemonSet (`daemonset.go:54`) | Create events; re-polls every 1m. |
| Pricing (`pricing.go:55`) | Hourly ClusterCost refresh. Never marks the cluster unconsolidated. |

**How pod and node state is ingested**

- `UpdateNode` (`state/cluster.go:416`) calls `newStateFromNode` (`:793-825`), which **rebuilds every per-pod map from
  scratch**. It lists pods by `spec.nodeName` (`populateResourceRequests`, `:932-949`).
- `UpdateNodeClaim` (`:383-406`) calls `newStateFromNodeClaim` (`:743-770`), which **carries the old maps forward but
  not `podDisruptionCosts`** (`:747-760`).
  - After any NodeClaim update, that node's `DisruptionCost()` therefore falls to 1.0 until the next Node update,
    which rebuilds every pod's entry. A pod update on that node restores only that pod's entry (`statenode.go:577-586`).
  - This matters to Balanced totals for non-candidate nodes (section 4.3).
- `UpdatePod` (`:451-463`) handles terminal pods through `updateNodeUsageFromPodCompletion` and all others through
  `updateNodeUsageFromPod` (`:953-970`).
- `StateNode.updateForPod` (`state/statenode.go:561-590`) records per pod:
  - requests and limits;
  - DaemonSet requests separately (`:571-574`);
  - eviction cost for non-DaemonSet pods, only when it is greater than 0 (`:577-586`);
  - host ports and volumes.

**What bumps the consolidation timestamp.** `ConsolidationState()` (`state/cluster.go:649-662`) returns
`clusterState`. If more than 5 minutes have passed, it instead calls `MarkUnconsolidated()` (`:636-642`), so **reading
the value can change it**. The value is bumped by:

- `DeletePod` (`:622`)
- a new pod binding (`cleanupOldBindings`, `:1003`)
- NodeClaim cleanup (`:782`) and Node cleanup (`:838`)
- `triggerConsolidationOnChange` (`:1018-1036`), but only when a node appears or disappears, is mocked, or its
  `Initialized()` or `MarkedForDeletion()` value flips
- a NodePool generation change (`informer/nodepool.go:78`)
- VPA prediction changes (`informer/vpa.go:76`, `:99`)
- node overlays (`pkg/controllers/nodeoverlay/controller.go:129`)

**What does not bump it**, so the change waits for the 5-minute reset:

- terminal pods arriving through `UpdatePod` (`:456-457`)
- `MarkForDeletion` and `UnmarkForDeletion`
- label and taint changes
- PDB changes and price refreshes
- a NodeClaim gaining `Consolidatable=True` (`pkg/controllers/nodeclaim/disruption/consolidation.go:80`)

The last item has a visible effect, through this sequence:

1. A pass finds candidates but no move, so it marks the cluster consolidated. `markConsolidated` runs only inside
   `ComputeCommands`, which `disrupt` calls only when some candidate exists (`controller.go:212-214`).
2. At the time of that pass, another node was still inside its `consolidateAfter` window.
3. That node later becomes consolidatable. Nothing bumps the timestamp, so both consolidation methods keep
   short-circuiting until the 5-minute reset.

---

## 2. Key types and signatures

All snippets below are quoted exactly from `32fd95b`. Only comments inside struct bodies are trimmed.

### 2.1 Method and its options

```go
// disruption/types.go:70-76
type Method interface {
	ShouldDisrupt(context.Context, *Candidate) bool
	ComputeCommands(context.Context, map[string]int, ...*Candidate) ([]Command, error)
	Reason() v1.DisruptionReason
	Class() string
	ConsolidationType() string
}

// disruption/types.go:78
type CandidateFilter func(context.Context, *Candidate) bool

// disruption/types.go:60-68
type MethodOptions struct {
	validator Validator
}

func WithValidator(v Validator) option.Function[MethodOptions] {
	return func(o *MethodOptions) {
		o.validator = v
	}
}

// disruption/controller.go:74-82
type ControllerOptions struct {
	methods []Method
}

func WithMethods(methods ...Method) option.Function[ControllerOptions] {
	return func(o *ControllerOptions) {
		o.methods = methods
	}
}

// disruption/controller.go:84-85
func NewController(ctx context.Context, clk clock.Clock, kubeClient client.Client, provisioner *provisioning.Provisioner,
	cp cloudprovider.CloudProvider, recorder events.Recorder, cluster *state.Cluster, queue *Queue, clusterCost *cost.ClusterCost, opts ...option.Function[ControllerOptions]) *Controller

// disruption/controller.go:102
func NewMethods(ctx context.Context, clk clock.Clock, cluster *state.Cluster, kubeClient client.Client, provisioner *provisioning.Provisioner, cp cloudprovider.CloudProvider, recorder events.Recorder, queue *Queue) []Method

// disruption/consolidation.go:75-76
func MakeConsolidation(clock clock.Clock, cluster *state.Cluster, kubeClient client.Client, provisioner *provisioning.Provisioner,
	cloudProvider cloudprovider.CloudProvider, recorder events.Recorder, queue *Queue) consolidation
```

**Classes** (`types.go:51-58`):

- `GracefulDisruptionClass = metrics.TerminationModeGraceful`
- `EventualDisruptionClass = metrics.TerminationModeEventual`
- `RepairDisruptionClass = "repair"`

**Reasons** (`pkg/apis/v1/nodepool.go:179-190`): `Underutilized`, `Empty`, `Drifted` and `Unhealthy`. The CEL enum at
`:180` fixes this set, so a new method must reuse one of them for its budgets.

**Method values in the default order** (`controller.go:108-121`)

| # | Method | Reason | Class | ConsolidationType | Source |
| --- | --- | --- | --- | --- | --- |
| 1 | Repair (only if `NodeRepair && !LegacyNodeRepair`, `:108-110`) | Unhealthy | repair | "" | `repair.go:361-365` |
| 2 | Emptiness (`:113`) | Empty | graceful | "empty" | `emptiness.go:112-122` |
| 3 | StaticDrift (`:115`, unconditional) | Drifted | eventual | "" | `staticdrift.go:167-177` |
| 4 | Drift (`:117`) | Drifted | eventual | "" | `drift.go:123-133` |
| 5 | MultiNodeConsolidation (`:119`) | Underutilized | graceful | "multi" | `multinodeconsolidation.go:245-255` |
| 6 | SingleNodeConsolidation (`:121`) | Underutilized | graceful | "single" | `singlenodeconsolidation.go:126-136` |

Notes on construction:

- `NewRepair` panics if `cluster.RepairPolicyMatcher()` is nil (`repair.go:63-67`).
- `NodeRepair` is alpha and off by default (`pkg/operator/options/options.go:90-96`).
- StaticDrift is registered without any gate check. Its `ShouldDisrupt` selects only static pools that have drifted
  (`staticdrift.go:56-58`).
- The `StaticCapacity` gate only adds the static provisioning controllers (`pkg/controllers/controllers.go:193-196`).

Feature gates that change behavior **inside** a method rather than the method order:

| Gate | Where it is read |
| --- | --- |
| `TerminateFirstDrift` | `drift.go:87`, `staticdrift.go:92`, `staticdrift.go:151` |
| `TerminateFirstRepair` | `repair.go:175` |
| `SpotToSpotConsolidation` | `consolidation.go:264` |
| `PredictionEnabled` | `controller.go:175` |

The test suite uses a different method list. `NewMethodsWithNopValidator()` (`disruption/validation_test.go:109-121`)
returns StaticDrift, Drift, Emptiness, MultiNode and SingleNode, with no Repair, using `NopValidator{}`
(`validation_test.go:103-107`). `suite_test.go:152` installs it, and `NewMethodsWithRealValidator()`
(`validation_test.go:99-101`) returns the production list. A parity test has to pin which order it compares against.

### 2.2 Candidate

```go
// disruption/types.go:82-108 (field comments trimmed)
type Candidate struct {
	*state.StateNode
	instanceType      *cloudprovider.InstanceType
	NodePool          *v1.NodePool
	zone              string
	capacityType      string
	DisruptionCost    float64
	reschedulablePods []*corev1.Pod
	hasPodBlockers    bool

	Price float64
	RescheduleDisruptionCost float64
	TerminationGracePeriod *time.Duration
	RepairPolicyResult health.RepairResult
	RebootEscalated bool
}

// disruption/types.go:134, 138, 146
func (c *Candidate) SavingsRatio() float64
func (c *Candidate) OwnedByStaticNodePool() bool
func (c *Candidate) IsEmpty() bool

// disruption/types.go:151-153
func NewCandidate(ctx context.Context, kubeClient client.Client, recorder events.Recorder, clk clock.Clock, node *state.StateNode, pdbs pdb.Limits,
	nodePoolMap map[string]*v1.NodePool, nodePoolToInstanceTypesMap map[string]map[string]*cloudprovider.InstanceType, queue *Queue, disruptionClass string,
) (*Candidate, error)
```

**How each field is computed** (`types.go:213-227`)

| Field | Computation |
| --- | --- |
| `*state.StateNode` | The deep copy from `DeepCopyNodes()`. |
| `instanceType` | `nodePoolToInstanceTypesMap[pool][Labels()["node.kubernetes.io/instance-type"]]` (`:194`). It may be nil; consolidation's `ShouldDisrupt` rejects nil. |
| `NodePool` | `nodePoolMap[Labels()["karpenter.sh/nodepool"]]`: one shared, cache-backed pointer per pool per call, to be treated as read-only (`:185-186`). |
| `zone`, `capacityType` | Taken from the labels (`:219-220`). |
| `DisruptionCost` | `ReschedulingCost(ctx, pods) * LifetimeRemaining(clk, nodePool, nodeClaim)` over **all** pods (`:224`). This is the legacy value; only Repair's sort uses it (`repair.go:137-138`). |
| `reschedulablePods` | `lo.Filter(pods, pod.IsReschedulable)` (`:213`): active pods (or a terminating StatefulSet pod) that are neither DaemonSet-owned nor mirror pods (`pkg/utils/pod/scheduling.go:45-52`). |
| `Price` | `cloudprovider.PriceNodeClaim(ctx, node.NodeClaim, instanceType)` (`:214`). Unknown, NaN, Inf or negative prices become 0 (`cloudprovider/types.go:919-925`). |
| `RescheduleDisruptionCost` | `1.0 + Σ max(0, EvictionCost(p))` over reschedulable pods (`:226`; `pkg/utils/disruption/disruption.go:51-57`). |
| `hasPodBlockers`, `TerminationGracePeriod`, `RepairPolicyResult`, `RebootEscalated` | Repair only. |

Helper methods:

- `SavingsRatio()` is `Price / RescheduleDisruptionCost`. It **panics** when the denominator is 0
  (`disruption.go:61-66`), so test fixtures must keep the cost at least 1.
- `IsEmpty()` is `RescheduleDisruptionCost <= PerNodeBaseDisruptionCost (1.0)` (`types.go:146-148`;
  `disruption.go:37`).

**`NewCandidate` rejection order** (`types.go:154-212`). Each check below rejects the node; the ones marked "Blocked
event" also publish one.

1. The node is already in the queue (`:155-157`). No event.
2. Repair class only: the node has no NodePool label (`:158-162`). Blocked event.
3. `node.ValidateNodeDisruptable(clk)` returns an error (`:164`; `state/statenode.go:286-321`). It checks in order:
   1. no NodeClaim
   2. no Node
   3. a reboot is in progress
   4. `MarkedForDeletion`
   5. not initialized, which is `NodeUninitializedError` when the node is registered
   6. `Nominated(clk)`
   7. the node annotation `karpenter.sh/do-not-disrupt=true`
   8. no NodePool label

   Repair ignores the do-not-disrupt and uninitialized errors (`:168-170`). Any other error publishes a Blocked event,
   but only for managed nodes (`:173-175`).
4. The NodePool or its instance-type map is missing (`:186-192`). Blocked event.
5. `node.ValidatePodsDisruptable(...)` fails (`:195`; `statenode.go:328-348`). It checks the pod do-not-disrupt
   annotation (including DaemonSet and mirror pods, `:336`), then `pdbs.CanEvictPods` (`:340`).
   - Repair keeps the node and records `hasPodBlockers` (`:202-204`).
   - The Eventual class ignores the failure when `NodeClaim.Spec.TerminationGracePeriod != nil` (`:205-206`).
   - Otherwise the failure publishes a Blocked event and rejects the node (`:208-211`).

A `pipeline` subpackage cannot read the unexported fields. The working tree adds exported accessors in an additive
file in package `disruption` (`pipeline_hooks.go`): `ReschedulablePods()`, `InstanceType()`, `Zone()` and
`CapacityType()`.

### 2.3 Command, Decision and Replacement

```go
// disruption/types.go:244-261 (field comments trimmed)
type Command struct {
	Method

	Succeeded bool

	CreationTimestamp time.Time
	ID                uuid.UUID

	Results             pscheduling.Results
	Candidates          []*Candidate
	Replacements        []*Replacement
	PoolDisruptionCosts map[string]float64
	TerminateFirstReason TerminateFirstReason
	Reboot bool
}

// disruption/types.go:230-242
type Replacement struct {
	*pscheduling.NodeClaim

	Name string
	Initialized bool
}

func replacementsFromNodeClaims(newNodeClaims ...*pscheduling.NodeClaim) []*Replacement {
	return lo.Map(newNodeClaims, func(n *pscheduling.NodeClaim, _ int) *Replacement { return &Replacement{NodeClaim: n} })
}

// disruption/types.go:271-285
type Decision string

var (
	NoOpDecision    Decision = "no-op"
	ReplaceDecision Decision = "replace"
	DeleteDecision  Decision = "delete"
	TerminateFirstDecision Decision = "terminate-first"
	RebootDecision         Decision = "reboot"
	ApprovedDecision Decision = "approved"
	RejectedDecision Decision = "rejected"
)

// scheduling/scheduler.go:320-325
type Results struct {
	NewNodeClaims              []*NodeClaim
	ExistingNodes              []*ExistingNode
	PodErrors                  map[*corev1.Pod]error
	DRAClaimAllocationMetadata map[types.NamespacedName]*dynamicresources.ResourceClaimAllocationMetadata
}
```

**`func (c Command) Decision() Decision`** (`types.go:298-312`) checks in order:

1. candidates and `Reboot`: reboot
2. candidates and replacements: replace
3. candidates and no replacements: `terminate-first` if `TerminateFirstReason != ""`, otherwise delete
4. otherwise: no-op

`TerminateFirstReason` takes the values `"no-reserved-capacity"` and `"static-at-limit"` (`types.go:288-296`).
`Command.Reason()` returns `""` when `Method` is nil (`:264-269`).

**Command helper methods**

| Method | Line |
| --- | --- |
| `PoolDisruptionCost(poolName string) float64` | `types.go:317` |
| `computePoolDisruptionCosts([]*Candidate) map[string]float64` | `:333` |
| `SourceNodeNames() []string` | `:345` |
| `String()` | `:352` |
| `StringForNode(*Candidate)` | `:391` |
| `SourceCost() float64` (Σ Price) | `:400` |
| `EstimatedSavings() float64` | `:409-435` |
| `EmitCandidateEvents` | `:438` |
| `EmitRejectedEvents` | `:445` |
| `LogValues() []any` | `:451` |

`EstimatedSavings` returns 0 for a reboot and `SourceCost` for a delete. For a replace it returns `SourceCost` minus,
for each `Results.NewNodeClaims`, the cheapest price among
`InstanceTypeOptions[0].Offerings.Launchable().Compatible(Requirements)`.

**Identity and pointer facts the pipeline relies on**

- `pscheduling.NodeClaim` embeds `NodeClaimTemplate`, which carries `NodePoolName`, `NodePoolUUID`, `NodePoolWeight`,
  `InstanceTypeOptions`, `Requirements` and `IsStaticNodeClaim` (`scheduling/nodeclaimtemplate.go:55-64`). It also
  holds `Pods` (`scheduling/nodeclaim.go:43-66`).
- `Replacements[0].NodeClaim` and `Results.NewNodeClaims[0]` are the **same pointer**, so filtering one filters the
  other.
- `Replacement.Name` is filled in after creation (`queue.go:355-357`).
- `ExistingNodes` holds every active non-candidate node, not just those that received pods (`scheduler.go:546`).
  `ExistingNode.Pods` holds only the pods placed in this simulation (`existingnode.go:179`).
- The `Pods` slices hold **deep copies** (`scheduler.go:515`). `PodErrors` is keyed by the **original** pointers,
  except for uninitialized-node errors (`helpers.go:153-162`) and minValues truncation errors
  (`scheduler.go:467-469`), which are keyed by the copies.
- **Compare by ObjectKey or UID, never by pointer.**

### 2.4 Evaluator, Validator and scoring types

```go
// disruption/balanced.go:191-195
type Evaluator interface {
	ApproveCommand(ctx context.Context, cmd Command) (bool, map[string]ScoreResult)
	CanPassThreshold(c *Candidate) bool
	EmitMultiNodeEvents(ctx context.Context, cmd Command, perPool map[string]ScoreResult, approved bool)
}

// disruption/consolidation.go:67-69
type NodePoolTotalsSetter interface {
	SetNodePoolTotals(map[string]NodePoolTotals)
}

// disruption/balanced.go:34-37
type NodePoolTotals struct {
	TotalCost           float64
	TotalDisruptionCost float64
}

// disruption/types.go:111-115
type ScoreResult struct {
	SavingsFraction    float64
	DisruptionFraction float64
	K                  int32
}

// disruption/balanced.go:109, 125, 131, 215
func ScoreMove(savings float64, disruptionCost float64, totals NodePoolTotals, k int32) ScoreResult
func ComputeMoveDisruptionCost(candidates []*Candidate) float64
func EvaluateBalancedMove(ctx context.Context, cmd Command, nodePoolTotals map[string]NodePoolTotals) (bool, map[string]ScoreResult)
func NewBalancedEvaluator(totals map[string]NodePoolTotals, recorder events.Recorder) Evaluator

// disruption/validation.go:92-94
type Validator interface {
	Validate(context.Context, Command, time.Duration) (Command, error)
}
```

**Validation error types** (`validation.go:37-90`). Every subtype embeds `*ValidationError` and implements `Unwrap()`,
so `IsValidationError` (`errors.As`, `:45-51`) matches all of them. `getValidationFailureReason` maps each type to a
reason string (`:359-378`):

| Type | Line | Reason string |
| --- | --- | --- |
| `ValidationError` | `:37` | "unknown" |
| `BudgetValidationError` | `:54` | "budget" |
| `SchedulingValidationError` | `:67` | "scheduling" |
| `ChurnValidationError` | `:80` | "churn" |

### 2.5 Functions on the decision path

```go
// disruption/helpers.go:262-264
func GetCandidates(ctx context.Context, cluster *state.Cluster, kubeClient client.Client, recorder events.Recorder, clk clock.Clock,
	cloudProvider cloudprovider.CloudProvider, shouldDisrupt CandidateFilter, disruptionClass string, queue *Queue,
) ([]*Candidate, error)

// disruption/helpers.go:273-275
func GetCandidatesWithTotals(ctx context.Context, cluster *state.Cluster, kubeClient client.Client, recorder events.Recorder, clk clock.Clock,
	cloudProvider cloudprovider.CloudProvider, shouldDisrupt CandidateFilter, disruptionClass string, queue *Queue, clusterCost *cost.ClusterCost,
) ([]*Candidate, map[string]NodePoolTotals, error)

// disruption/helpers.go:311
func BuildNodePoolMap(ctx context.Context, kubeClient client.Client, cloudProvider cloudprovider.CloudProvider) (map[string]*v1.NodePool, map[string]map[string]*cloudprovider.InstanceType, error)

// disruption/helpers.go:346, 350, 370, 417
func NodePoolStats(cluster *state.Cluster, reason v1.DisruptionReason) (numNodes, disrupting map[string]int)
func NodePoolBudgetMap(ctx context.Context, clk clock.Clock, nodePools map[string]*v1.NodePool, numNodes, disrupting map[string]int, reason v1.DisruptionReason) map[string]int
func NodePoolStatsFromNodes(nodes []*state.StateNode, reason v1.DisruptionReason) (numNodes, disrupting map[string]int)
func BuildDisruptionBudgetMapping(ctx context.Context, cluster *state.Cluster, clk clock.Clock, kubeClient client.Client, cloudProvider cloudprovider.CloudProvider, recorder events.Recorder, reason v1.DisruptionReason) (map[string]int, error)

// disruption/helpers.go:56-59, 64-66
type SimulationOptions struct {
	IncludeBlockedCandidatePods bool
}
func SimulateScheduling(ctx context.Context, kubeClient client.Client, cluster *state.Cluster, provisioner *provisioning.Provisioner, clk clock.Clock, recorder events.Recorder,
	schedulerOpts []scheduling.Options, simulationOpts SimulationOptions, candidates ...*Candidate,
) (scheduling.Results, error)

// disruption/consolidation.go:149, 159, 261
func (c *consolidation) sortCandidates(_ context.Context, candidates []*Candidate) []*Candidate
func (c *consolidation) computeConsolidation(ctx context.Context, candidates ...*Candidate) (Command, error)
func (c *consolidation) computeSpotToSpotConsolidation(ctx context.Context, candidates []*Candidate, results pscheduling.Results, candidatePrice float64) (Command, error)

// disruption/singlenodeconsolidation.go:53, 139, 144
func (s *SingleNodeConsolidation) ComputeCommands(ctx context.Context, disruptionBudgetMapping map[string]int, candidates ...*Candidate) ([]Command, error)
func (s *SingleNodeConsolidation) SortCandidates(ctx context.Context, candidates []*Candidate) []*Candidate
func (s *SingleNodeConsolidation) shuffleCandidates(ctx context.Context, nodePoolCandidates map[string][]*Candidate) []*Candidate

// disruption/multinodeconsolidation.go:50, 115, 207
func (m *MultiNodeConsolidation) ComputeCommands(ctx context.Context, disruptionBudgetMapping map[string]int, candidates ...*Candidate) ([]Command, error)
func (m *MultiNodeConsolidation) firstNConsolidationOption(ctx context.Context, candidates []*Candidate, max int) (Command, map[string]ScoreResult, error)
func filterOutSameInstanceType(replacement *Replacement, consolidate []*Candidate) (*Replacement, error)

// disruption/queue.go:365, 480
func (q *Queue) StartCommand(ctx context.Context, cmd *Command) error
func (q *Queue) HasAny(ids ...string) bool

// provisioner.go:190, 282-288
func (p *Provisioner) CreateNodeClaims(ctx context.Context, nodeClaims []*scheduler.NodeClaim, opts ...option.Function[LaunchOptions]) ([]string, error)
func (p *Provisioner) NewScheduler(
	ctx context.Context,
	pods []*corev1.Pod,
	stateNodes []*state.StateNode,
	deletingPodUIDs sets.Set[types.UID],
	opts ...scheduler.Options,
) (*scheduler.Scheduler, error)
```

### 2.6 `state.Cluster` and `StateNode` getters

`NewCluster(clk clock.Clock, client client.Client, cloudProvider cloudprovider.CloudProvider, opts ...option.Function[ClusterOptions]) *Cluster`
is at `state/cluster.go:120`.

**`Cluster` struct fields that matter here** (`state/cluster.go:56-106`)

- `nodes map[string]*StateNode` (`:63`) is keyed by **providerID**.
- `bindings` (pod key -> node name, `:64`) has no public getter.
- `nodePoolResources` (`:67`) and `daemonSetPods sync.Map` (`:68`).
- `NodePoolState` (`:72`).
- `clusterState time.Time` (`:86`) has its own mutex (`:80`).
- `antiAffinityPods sync.Map` (`:91`).
- `bufferPodCounts` (`:105`). Only Emptiness reads it.

**`func (c *Cluster) ...` getters**

| Signature | Line | Notes |
| --- | --- | --- |
| `Synced(ctx context.Context) (synced bool)` | 152 | Has side effects: sets the `ClusterStateSynced` metrics (`:154-180`). |
| `ForPodsWithAntiAffinity(fn func(p *corev1.Pod, n *corev1.Node) bool)` | 252 | Live read through `bindings`. The scheduler's topology reads it (`scheduling/topology.go:315`). |
| `Nodes() iter.Seq[*StateNode]` | 272 | Yields **live pointers** under the RLock, in map order. Do not store them. |
| `DeepCopyNodes() StateNodes` | 286 | `lo.Map(lo.Values(c.nodes), DeepCopy)` (`:290`), in **random order**. |
| `IsNodeNominated(providerID string) bool` | 302 | `n.Nominated(c.clock)` using the cluster's injected clock. |
| `NominateNodeForPod(ctx context.Context, providerID string)` | 313 | Called only from `Results.Record`. |
| `HasBufferPods(providerID string) bool` | 338 | Used by Emptiness (`emptiness.go:54`). |
| `UnmarkForDeletion(providerIDs ...string)` | 350 | Flips the flag in place and updates `NodePoolState`. Does not call `MarkUnconsolidated`. |
| `MarkForDeletion(providerIDs ...string)` | 367 | Same as above. |
| `NodeClaimExists(nodeClaimName string) bool` | 465 | Used by the queue. |
| `MarkPodSchedulingDecisions(ctx context.Context, podErrors map[*corev1.Pod]error, npPods map[string][]*corev1.Pod, ncPods map[string][]*corev1.Pod)` | 503 | A write that disruption reaches through `GetPendingPods` (`provisioner.go:223`). |
| `MarkUnconsolidated() time.Time` | 636 | `clusterState = clock.Now()`. |
| `ConsolidationState() time.Time` | 649 | Resets itself after 5 minutes (section 1.4). |
| `NodePoolResourcesFor(nodePoolName string) corev1.ResourceList` | 664 | Returns `maps.Clone`. |
| `GetDaemonSetPod(daemonset *appsv1.DaemonSet) *corev1.Pod` | 703 | Returns a deep copy. |
| `Reset()`, `SetSynced(bool)`, `HasSynced()` | 672, 697, 1039 | Test-only. |

**`func (in *StateNode) ...` getters** (`state/statenode.go`). The struct is at `:179-202`, with public `Node` and
`NodeClaim` fields. `DeepCopy()` (`zz_generated.deepcopy.go:145`) copies every map and both usage objects.
`ShallowCopy()` (`:216`) shares everything.

| Signature | Line | Notes |
| --- | --- | --- |
| `Name() string` | 250 | The NodeClaim name until the node registers. |
| `ProviderID() string` | 266 | |
| `Pods(ctx context.Context, kubeClient client.Client) ([]*corev1.Pod, error)` | 274 | Reads the **kube cache** (a `spec.nodeName` index), not state. |
| `ValidateNodeDisruptable(clk clock.Clock) error` | 286 | |
| `ValidatePodsDisruptable(ctx context.Context, kubeClient client.Client, pdbs pdb.Limits, clk clock.Clock, recorder events.Recorder) ([]*corev1.Pod, error)` | 328 | Returns all pods even when it also returns an error. |
| `CurrentlyReschedulablePods(ctx context.Context, kubeClient client.Client, clk clock.Clock, recorder events.Recorder) ([]*corev1.Pod, error)` | 351 | Builds its own `pdb.NewLimits` on every call. |
| `HostName() string` | 358 | |
| `Annotations() map[string]string`, `Labels() map[string]string` | 365, 380 | Return the underlying map, not a copy. |
| `RebootInProgress() bool` | 398 | |
| `Taints() []corev1.Taint` | 406 | For managed nodes that are uninitialized or rebooting, strips known ephemeral taints, the reboot `NoSchedule` fence and startup taints (`:416-435`). |
| `Registered() bool`, `Initialized() bool` | 440, 449 | Label-based. Always true for unmanaged nodes. |
| `Capacity()`, `Allocatable()`, `Available() corev1.ResourceList` | 458, 476, 494 | `Available` is allocatable minus `PodRequests()`, and **includes DaemonSet pods**. |
| `DaemonSetRequests()`, `DaemonSetLimits()` | 498, 502 | |
| `HostPortUsage() *scheduling.HostPortUsage`, `VolumeUsage() *scheduling.VolumeUsage` | 506, 510 | **Mutable pointers**: `ExistingNode` mutates them (`existingnode.go:51`, `:183-184`). |
| `PodRequests()`, `PodLimits()` | 514, 522 | |
| `DisruptionCost() float64` | 528 | `1.0 + Σ podDisruptionCosts`. |
| `MarkedForDeletion() bool`, `Deleted() bool` | 536, 544 | `MarkedForDeletion` is `markedForDeletion` OR `Deleted()`. |
| `Nominate(ctx context.Context, clk clock.Clock)`, `Nominated(clk clock.Clock) bool` | 549, 553 | |
| `Managed() bool` | 557 | `NodeClaim != nil`. |

`StateNodes` helpers: `Active()` (`:136`), `Deleting()` (`:143`), `Pods` (`:150`) and `CurrentlyReschedulablePods`
(`:162`).

---

## 3. Single-node and multi-node consolidation

### 3.1 The shared `consolidation` value

**The struct** (`consolidation.go:51-64`) has these fields: `queue`, `clock`, `cluster`, `kubeClient`, `provisioner`,
`cloudProvider`, `recorder`, `lastConsolidationState time.Time` and `evaluator Evaluator`.

- `MakeConsolidation` returns a **value**, and the evaluator starts as `noopEvaluator{}` (`:75-87`).
- `NewMethods` builds one value (`controller.go:103`), and every method that embeds it gets its **own copy**:
  Emptiness (`emptiness.go:32`), Multi (`multinodeconsolidation.go:37`), Single (`singlenodeconsolidation.go:37`) and
  Repair (`repair.go:55`).
- Each copy therefore has its own `lastConsolidationState` and its own evaluator.
- Each validator is built from yet another copy (`validation.go:116-132`, `:156-190`).

**Constants**

- `commandValidationDelay = 15 * time.Second` (`:45`)
- `MinInstanceTypesForSpotToSpotConsolidation = 15` (`:48`)

**The consolidated short-circuit**

- `IsConsolidated()` is `lastConsolidationState.Equal(cluster.ConsolidationState())` (`:90-92`).
- `markConsolidated()` stores the current value (`:95-97`).
- Every consolidation `ComputeCommands` returns `[]Command{}` right away when `IsConsolidated()` is true: single
  `:54-56`, multi `:51-53`, emptiness `:65-67`.
- `GetCandidatesWithTotals` still runs before `ComputeCommands`, so candidate construction, PDB listing and Blocked
  events happen on every pass.

**`func (c *consolidation) ShouldDisrupt(ctx context.Context, cn *Candidate) bool`** (`consolidation.go:100-136`) is
shared by Multi and Single. It rejects in this order:

1. a static NodePool (`:102-104`), with no event
2. `instanceType == nil` (`:110-113`), with an Unconsolidatable event
3. no `karpenter.sh/capacity-type` label (`:114-117`), with an event
4. no zone label (`:118-121`), with an event
5. `ConsolidateAfter.Duration == nil` (`:122-125`), with an event
6. `IsEmpty()` (`:127-129`), with no event. Empty nodes belong to Emptiness, under the `Empty` reason and budgets.
7. policy `WhenEmpty` (`:131-134`), with an event

It then returns `NodeClaim.StatusConditions().Get(ConditionTypeConsolidatable).IsTrue()` (`:135`).
`WhenEmptyOrUnderutilized` and `Balanced` both pass this filter; only the evaluator tells them apart.

**The `Consolidatable` condition** (`pkg/controllers/nodeclaim/disruption/consolidation.go:40-85`)

- It is cleared when `consolidateAfter` is nil (`:45-51`) or the NodeClaim is not initialized (`:54-60`).
- It is cleared while `IsUnderConsolidateAfter` holds, with a requeue at `timeToCheck + consolidateAfter` (`:65-77`).
  `timeToCheck` is `LastPodEventTime`, or the Initialized transition time when that is zero (`:63`).
- Otherwise it is set true (`:80`).
- `IsUnderConsolidateAfter` returns false when `consolidateAfter` is 0 (`pkg/utils/disruption/disruption.go:141-155`).

**Emptiness, briefly**

- Its `ShouldDisrupt` (`emptiness.go:42-59`) rejects static pools, a nil `consolidateAfter` (with an event) and nodes
  with buffer pods (with an event). It then requires `IsEmpty() && Consolidatable`.
- It runs for all three policies and has no instance-type, capacity-type or zone checks.
- `ComputeCommands` (`:64-110`) runs these steps:
  1. `sortCandidates`. Every empty node costs exactly 1.0, so the sort is effectively price-descending.
  2. A budget filter that decrements the mapping in place (`:72-85`).
  3. If nothing was empty, `markConsolidated` unless a budget blocked a candidate (`:87-95`).
  4. One DELETE command over **all** remaining empty nodes, with no evaluator call (`:97-100`).
  5. Validation by the `EmptinessValidator` (section 3.8). A validation error is logged and returns no commands
     (`:101-107`).

### 3.2 `sortCandidates`

`func (c *consolidation) sortCandidates(_ context.Context, candidates []*Candidate) []*Candidate` (`consolidation.go:149-154`):

```go
sort.Slice(candidates, func(i, j int) bool {
	return candidates[i].SavingsRatio() > candidates[j].SavingsRatio()
})
```

- The order is **descending** by `Price / RescheduleDisruptionCost`.
- The sort happens **in place** on the caller's slice, using `sort.Slice`, which is **unstable**, and there is **no
  tie-breaker**.
- The input order comes from `DeepCopyNodes()`, which is map order. Tied candidates therefore come out in a
  run-dependent order. Ties are common: identical nodes, or empty nodes at the same price.
- The key does not depend on the clock.
- The comment at `consolidation.go:143-148` records that this replaced an ascending sort by `DisruptionCost`. The doc
  comment on `firstNConsolidationOption` (`multinodeconsolidation.go:112-113`) still describes the old order and is
  stale.

### 3.3 `computeConsolidation`

`func (c *consolidation) computeConsolidation(ctx context.Context, candidates ...*Candidate) (Command, error)`
(`consolidation.go:159-254`) returns `Command{Candidates, Replacements, Results, PoolDisruptionCosts}`. `Method`, `ID`
and the timestamp are added later, by the controller. The function runs these steps:

1. **Simulate** (`:162`). See the `SimulateScheduling` walkthrough below.
2. **Errors.**
   - `errCandidateDeleting` returns `Command{}, nil`, meaning "retry later" (`:165-167`).
   - Any other error returns `Command{}, err` unchanged; `SimulateScheduling` has already wrapped it with `%w`
     (`helpers.go:141`). This is the only error return, and it is how a multi-node deadline reaches its caller
     (`:168`).
3. **Unscheduled pods.** If `!results.AllNonPendingPodsScheduled()`, return no-op. Pods that were already pending are
   ignored (`scheduler.go:427-431`). A single-candidate call publishes Unconsolidatable with
   `NonPendingPodSchedulingErrors()` (`:172-178`).
4. **DELETE.** If `len(results.NewNodeClaims) == 0`, return `Command{Candidates, Results, PoolDisruptionCosts}`
   (`:181-187`). This path emits no candidate events.
5. **More than one new NodeClaim** returns no-op. It is never m -> n (`:190-195`).
6. `candidatePrice := sumCandidatePrices(candidates)` (`:199`; `balanced.go:186-188`). `allExistingAreSpot` is true
   when every candidate's `capacityType == spot` (`:201-206`).
7. **Order by price, in place.** `InstanceTypeOptions = InstanceTypeOptions.OrderByPrice(Requirements)` (`:210`).
   This is an unstable `sort.Slice` on the cheapest launchable, compatible offering (`cloudprovider/types.go:396-415`).
8. **Spot-to-spot branch.** If all candidates are spot and the requirement allows spot, return
   `computeSpotToSpotConsolidation` (`:212-215`).
9. **Price filter.**
   `RemoveInstanceTypeOptionsByPriceAndMinValues(Requirements, candidatePrice)` (`:221`;
   `scheduling/nodeclaim.go:457-469`) keeps an instance type only if
   `Offerings.Launchable().WorstLaunchPrice(reqs) < candidatePrice`.
   - `WorstLaunchPrice` returns the most expensive compatible offering of the **first** capacity-type class present,
     in the order reserved, spot, on-demand. It returns `MaxFloat64` when nothing is compatible
     (`cloudprovider/types.go:677-684`; `cloudprovider/capacitytype.go:43`).
   - The filter then checks `SatisfiesMinValues`. A failure gives no-op, plus the event "Filtering by price: ..." when
     there is one candidate (`:222-227`).
   - **An unknown source price (0) filters out every replacement.**
10. **Nothing left.** No options remain: no-op, plus "Can't replace with a cheaper node" for one candidate
    (`:228-233`).
11. **Pin to spot.** If the capacity-type requirement allows both spot and on-demand, add
    `capacity-type In [spot]` (`:240-243`). The price filter priced against spot, so this pins the launch to spot.
12. **REPLACE.** Build `Command{Candidates, Replacements: replacementsFromNodeClaims(...), Results, PoolDisruptionCosts}`
    and call `cmd.EmitCandidateEvents(c.recorder)` (`:245-253`).

**Side effects**

- The function mutates `results.NewNodeClaims[0]` in place: it reorders and filters `InstanceTypeOptions` and adds
  `Requirements`.
- It publishes Unconsolidatable events only when there is one candidate.
- It publishes ConsolidationCandidate events only on REPLACE (`types.go:438-442`).

**`computeSpotToSpotConsolidation`** (`consolidation.go:261-343`)

1. The `SpotToSpotConsolidation` gate is alpha and off by default (`pkg/operator/options/options.go:104-109`). When it
   is off, the result is no-op, plus an event when there is one candidate (`:264-269`).
2. Add `capacity-type In [spot]` (`:272`), then take `InstanceTypeOptions.Compatible(Requirements)` (`:274`).
   `Compatible` checks `Offerings.Available()`, not `Launchable` (`cloudprovider/types.go:418-426`), and keeps the
   price order.
3. Apply the same price filter, with the same minValues and empty exits as the main path (`:278-290`).
4. **Multi-node (more than one candidate):** return REPLACE right away. The 15-type rule does not apply (`:294-304`).
5. **Single-node:**
   - Fewer than 15 options remaining gives no-op plus an event (`:311-315`).
   - Otherwise truncate. With minValues, keep `[0 : max(15, minNeededFromSatisfiesMinValues)]` (`:326-329`). Without,
     keep `[0:15]` (`:331`).
   - The comment at `:317` says "cap ... at 100", but the code does not do that.
   - Build the command and emit candidate events (`:334-342`).

**Inside `SimulateScheduling`** (`helpers.go:64-168`)

1. Collect the candidate names (`:67`).
2. `nodes := cluster.DeepCopyNodes()` (`:68`). `deletingNodes := nodes.Deleting()` (`:69`).
3. `stateNodes = nodes.Active()` minus the candidates (`:70-72`). **Candidates are removed from the simulation, not
   marked.**
4. Return `errCandidateDeleting` if any candidate is already deleting (`:77-81`).
5. `provisioner.GetPendingPods(ctx)` (`:83`).
   - It lists unscheduled pods that `IsProvisionable` (`pkg/utils/node/node.go:190-197`;
     `pkg/utils/pod/scheduling.go:103-109`), then runs `Validate`.
   - **Side effects:** for invalid pods it calls `MarkPodSchedulingDecisions` (`provisioner.go:223`), publishes
     `PodFailedToScheduleEvent` (`:227`), sets `IgnoredPodCount` (`:233`) and logs `consolidationWarnings` (`:234`,
     `:247-277`).
   - With the CapacityBuffer gate on, it appends virtual buffer pods (`:238-240`).
6. Candidate pods are `candidate.reschedulablePods`, captured once in `NewCandidate`. The function then filters them
   through a fresh `pdb.NewLimits` and `IsCurrentlyReschedulable(p, clk, recorder)` (`:101-109`). Pods held by a fully
   blocking PDB are dropped from the simulation, but still count for topology at their old node.
7. Add the pods of deleting nodes: `deletingNodes.CurrentlyReschedulablePods(...)` (`:113`). The full pod list is
   pending + candidate + deleting-node pods (`:111`, `:117`).
8. Options, in order: `IgnorePreferences` (if the policy is Ignore), then `MinValuesPolicy`, then the caller's
   options (`:119-124`). `NumConcurrentReconciles` is not set, so the scheduler uses one worker.
9. `deletingPodUIDs` holds the candidate and deleting-node pod UIDs (`:127`). They are used for DRA and for the
   prediction store.
10. `provisioner.NewScheduler(nopLoggerCtx, pods, stateNodes, deletingPodUIDs, opts...)` (`:128-134`), then
    `Solve` (`:139`). **Any error, including `DeadlineExceeded`, discards the results** (`:140-142`). That differs from
    `Provisioner.Schedule`, which tolerates deadlines.
11. `TruncateInstanceTypes(ctx, 600)` (`:143`). It calls `InstanceTypeOptions.Truncate`, which sorts with
    `OrderByPrice`, slices, then checks minValues (`cloudprovider/types.go:502-514`). A claim that fails minValues moves
    its pods to `PodErrors` (`scheduler.go:458-476`).
12. Pods placed on **uninitialized** existing nodes get `UninitializedNodeError`, except pods from deleting nodes
    (`:144-166`). Managed nodes count as initialized only with the label `karpenter.sh/initialized=true`
    (`statenode.go:449-456`).

`Results.Record` is not called, so the simulation nominates nothing.

**Inside `Provisioner.NewScheduler`** (`provisioner.go:282-378`)

1. Load NodePools: managed only, dropping static, not-Ready and deleting pools (`:289-302`). Order them with
   `OrderByWeight`, which sorts by weight descending and then name descending, a deterministic order (`:310`;
   `pkg/utils/nodepool/nodepool.go:162-171`).
2. Call `GetInstanceTypes` per pool. `DeadlineExceeded` is fatal (`:312-331`).
3. Compute volume topology requirements (`:336`; `:612-630`).
4. Run the default spread-constraint injector. It mutates pods in place and is a no-op unless SchedulerConfig defaults
   are set (`pkg/controllers/provisioning/scheduling/defaultconstraints.go:106-113`, `:130`).
5. Build the topology: `scheduler.NewTopology(ctx, p.kubeClient, p.cluster, ...)` (`:350`). This uses **`p.cluster`**,
   not the cluster passed to `SimulateScheduling`.
6. Collect DaemonSet pods (`:354`, `:560-585`).
7. Build the DRA allocator, but only when `!IgnoreDRARequests`. The unit tests default to `true`
   (`pkg/test/options.go:97`), so the allocator is nil there.
8. Call `scheduler.NewScheduler(..., p.cluster, stateNodes, ..., p.clock, ...)` (`:377`).
   - Its constructor reads the live `cluster.Nodes()` to collect the names of MarkedForDeletion nodes
     (`scheduler.go:237-243`).
   - It builds existing nodes with `isUnderConsolidateAfter`. That value is only set under
     `IsConsolidationSimulation`, and it is computed on `p.clock` (`scheduler.go:837-847`).
   - It sorts existing nodes **stably**, initialized first and then by `Name()` (`scheduler.go:890-903`).

**`Solve`** (`scheduler.go:479-558`)

- `NewQueue` sorts the caller's pod slice in place by CPU descending, memory descending, creationTimestamp ascending
  and UID ascending, which is a total order (`scheduling/queue.go:37-38,73-108`).
- Each pod goes through `trySchedule(ctx, pod.DeepCopy())` (`:515`). On `DeadlineExceeded` the loop stops
  (`:516-518`), and `Solve` returns `ctx.Err()` (`:557`).
- `add` (`:627`) tries, in order:
  1. existing nodes, first fit by index
  2. an unstable sort of the new claims by pod count (`:648`)
  3. in-flight claims
  4. new claims, over templates in weight order

### 3.4 Single-node consolidation

**Struct and defaults**

- The struct (`singlenodeconsolidation.go:36-40`) holds the embedded `consolidation`, `PreviouslyUnseenNodePools
  sets.Set[string]` and `validator Validator`.
- The default validator is `NewSingleConsolidationValidator(c)` (`:43`).
- `var SingleNodeConsolidationTimeoutDuration = 3 * time.Minute` (`:33`) is a **var**, so tests can change it.

**Order: `SortCandidates`** (`:139-142`, `:144-170`)

1. Run `sortCandidates`, which is ratio-descending.
2. Group by NodePool with `lo.GroupBy`, which keeps the order within each pool.
3. Interleave round-robin with `shuffleCandidates`. Despite the name, it does not shuffle randomly.
   - The pool order is `PreviouslyUnseenNodePools.UnsortedList()` (`:150`) followed by `lo.Keys(nodePoolCandidates)`
     minus that set (`:151-153`). Both are **set and map iteration orders**.
   - Round *i* appends the *i*-th candidate of each pool, in that pool order (`:156-167`).

**`ComputeCommands`** (`:53-124`)

1. Return early if `IsConsolidated()` (`:54-56`).
2. Order the candidates (`:57`).
3. Set the deadline: `timeout := s.clock.Now().Add(SingleNodeConsolidationTimeoutDuration)`, on the **injected clock**
   (`:60`).
4. `unseenNodePools` starts as the set of all candidate pools (`:63`).
5. For each candidate in order (`:65-112`):
   1. **Timeout, checked before each evaluation** (`:66-73`). Past the deadline, increment
      `ConsolidationTimeoutsTotal{single}`, log, set `PreviouslyUnseenNodePools = unseenNodePools`, and return empty
      without marking consolidated. A single slow `computeConsolidation` call can run past the deadline.
   2. Remove the pool from `unseenNodePools` (`:75`). This happens **before** the budget check.
   3. **Budget.** If `disruptionBudgetMapping[pool] == 0`, set `constrainedByBudgets = true` and continue. The budget
      is **not decremented**, because a single-node command has one candidate (`:80-83`).
   4. **`CanPassThreshold`.** If false, continue (`:86-88`).
   5. `computeConsolidation(ctx, candidate)`. An error is **logged and skipped** (`:91-95`). A no-op continues
      (`:96-98`).
   6. **`ApproveCommand`.** If not approved, continue (`:100-102`).
   7. **`Validate(ctx, cmd, 15s)`** (`:103`).
      - A `ValidationError` calls `cmd.EmitRejectedEvents(recorder, reason)` and **returns empty**. It does not try the
        next candidate and does not mark consolidated (`:104-108`).
      - Any other error returns `"validating consolidation, %w"` (`:109`).
   8. Success returns `[]Command{cmd}`, the **first** command that validates (`:111`).
6. After a full pass: `markConsolidated()` unless budgets blocked a candidate (`:114-119`). Skips caused by errors or
   the threshold still allow marking. Then `PreviouslyUnseenNodePools = unseenNodePools`, which is empty after a full
   pass (`:121`).

### 3.5 Multi-node consolidation and the prefix binary search

`const MultiNodeConsolidationTimeoutDuration = 1 * time.Minute` (`multinodeconsolidation.go:34`) is a **const**.

**`ComputeCommands`** (`:50-110`)

1. Return early if `IsConsolidated()` (`:51-53`).
2. Run `sortCandidates` only, with no NodePool interleave (`:54`).
3. **Budget pre-filter, in sorted order** (`:63-75`).
   - A pool whose budget is 0 sets `constrainedByBudgets` and is skipped.
   - Otherwise the candidate is appended and **`disruptionBudgetMapping[pool]--`, which mutates the caller's map**.
   - Result: for each pool, only the top-`budget` candidates by ratio are eligible.
4. `maxParallel := lo.Clamp(len(disruptableCandidates), 0, 100)` (`:79`).
5. Call `firstNConsolidationOption(...)`. An error is returned (`:81-84`).
6. **No-op.** `markConsolidated()` unless budget-constrained, then return empty (`:86-94`). A timeout that saved
   nothing ends here too, and it **does** mark consolidated.
7. If `perPoolResults != nil`, call `EmitMultiNodeEvents(ctx, cmd, perPoolResults, true)` (`:97-99`). This happens
   before validation.
8. `if cmd, err = m.validator.Validate(...)` (`:101`). Validation behaves as it does for single-node, with one quirk.
   On failure `ConsolidationValidator.Validate` returns `Command{}` (`validation.go:193-195`), and that value is
   assigned back to `cmd`. `cmd.EmitRejectedEvents` at `:104` therefore loops over zero candidates, so **multi-node
   emits no `ConsolidationRejected` events**.

**`firstNConsolidationOption`** (`:115-189`)

- With fewer than 2 candidates, return no-op (`:117-119`).
- Set `min := 1`. If `len(candidates) <= max`, set `max = len-1` (`:120-123`).
- The deadline is `timeoutCtx, cancel := context.WithTimeout(ctx, MultiNodeConsolidationTimeoutDuration)`
  (`:132-133`). This is **wall clock**, not `m.clock`.
- While `min <= max` (`:134-181`):
  1. Compute `mid := (min+max)/2` and the window `candidates[0 : mid+1]` (`:135-136`).
  2. Call `computeConsolidation(timeoutCtx, window...)` (`:139`).
  3. On `context.DeadlineExceeded`, increment `ConsolidationTimeoutsTotal{multi}` and return `lastSavedCommand,
     lastSavedPerPool`, or no-op if nothing was saved (`:142-150`). Any other error is returned (`:152`).
  4. Start with `validDecision := cmd.Decision() == DeleteDecision` (`:156`). For a REPLACE, call
     `filterOutSameInstanceType(cmd.Replacements[0], window)`. The move is valid when that returns no error and leaves
     options (`:157-163`).
  5. If the move is valid, call `ApproveCommand`.
     - A rejection marks the move invalid and remembers `lastRejectedCmd/PerPool`.
     - An approval with a non-nil `perPool` sets `lastSavedPerPool` (`:165-173`).
  6. If valid, set `lastSavedCommand = cmd` and `min = mid+1`; otherwise set `max = mid-1` (`:174-180`).
- After the loop, if nothing was saved but something was rejected, call `EmitMultiNodeEvents(lastRejected..., false)`
  once (`:185-187`).

**What the search does and does not guarantee**

- **Window sizes** run from 2 to `len` when there are at most 100 disruptable candidates.
- **Off-by-one:** with more than 100, `max` stays 100, so the largest window is **101** candidates.
- **Cost:** the search runs about ⌈log2(len)⌉ simulations, at most 7.
- **Monotonicity:** the search assumes that if a prefix is feasible, every shorter prefix is too. It returns the last
  valid window it found, not necessarily the largest feasible one.
  - Rejections in intermediate windows go unrecorded when a smaller window later succeeds.
  - The RFC proves monotonicity for DELETEs only (`designs/balanced-consolidation.md:396-404`).
- **Example** with 10 disruptable candidates (min 1, max 9):
  1. The first window is 6 nodes (mid 5).
  2. If it is valid, the next window is 8 nodes (mid 7). If it is invalid, the next is 3 nodes (max 4, mid 2).
  3. The search continues until min > max.

**`filterOutSameInstanceType`** (`:207-243`) rejects replacements that only re-create an instance type that is being
removed.

1. `pricesByInstanceType[name]` is the lowest `Price` among the window's candidates of that type, skipping
   `Price <= 0` (`:212-224`).
2. `maxPrice` is the lowest of those prices over replacement options whose type also appears in the window
   (`:226-236`). If no type overlaps, `maxPrice = MaxFloat64`, and only types with no launchable offering are dropped.
3. `RemoveInstanceTypeOptionsByPriceAndMinValues(Requirements, maxPrice)` mutates the **shared** NodeClaim (`:238`), so
   the Balanced score that follows sees the filtered options. On error it returns `nil` (`:239-241`).
4. **Unknown-price trap:** if an overlapping type has only unknown prices, the map lookup returns 0, `maxPrice`
   becomes 0, and every option is removed.

### 3.6 Price and spot rules, summarized

| Rule | Where | Single | Multi |
| --- | --- | --- | --- |
| Order options by cheapest launchable compatible price | `consolidation.go:210` | yes | yes |
| Keep options whose worst launch price is below the summed source price | `consolidation.go:221` | yes | yes |
| Pin `[on-demand, spot]` replacements to spot | `consolidation.go:240-243` | yes | yes |
| Spot-to-spot requires the gate | `consolidation.go:264` | yes | yes |
| Spot-to-spot requires at least 15 cheaper types, truncated to 15 (or the minValues need) | `consolidation.go:311-332` | yes | no |
| Drop types at or above the cheapest same-type source | `multinodeconsolidation.go:157-163`, `:207-243` | no | yes |
| Validation: replacement types must be a subset of a fresh simulation's types | `validation.go:348`; `helpers.go:255-259` | yes | yes |

### 3.7 Budgets

**`BuildDisruptionBudgetMapping`** (`helpers.go:417-438`)

1. `numNodes, disrupting := NodePoolStats(cluster, reason)` (`:419`). This is `NodePoolStatsFromNodes(cluster.DeepCopyNodes(), reason)`
   (`:346-348`, `:370-413`).
   - It skips unmanaged nodes and nodes that are not initialized, unless a reboot is in progress (`:382`). It also
     skips nodes whose NodeClaim has `InstanceTerminating=True` (`:389`).
   - It counts `numNodes[pool]++` for the rest.
   - It counts `disrupting[pool]++` if the node is `MarkedForDeletion() || RebootInProgress() || (NotReady && reason != Unhealthy)`
     (`:405-410`).
2. List the managed NodePools (`:420`).
3. For each pool, set `mapping[name] = max(MustGetAllowedDisruptions(clk, numNodes[name], reason) - disrupting[name], 0)` (`:425-426`).
4. Side effects:
   - gauges `NodePoolAllowedDisruptions` and `NodePoolNodesConsumingBudgets` (`:427-432`)
   - the event `NodePoolBlockedForDisruptionReason` when `numNodes != 0 && allowed == 0` (`:433-435`)

**How the allowance is computed** (`pkg/apis/v1/nodepool.go`)

- `MustGetAllowedDisruptions` returns 0 on **any** error (`:359-365`).
- `GetAllowedDisruptionsByReason` takes the minimum over budgets whose `Reasons` is nil or contains the reason,
  starting from `MaxInt32` (`:368-381`).
  - It appends errors from budgets for **other** reasons too.
  - So one misconfigured budget anywhere sets the result to 0.
- `Budget.GetAllowedDisruptions` returns `MaxInt32` for an inactive schedule. Activity is evaluated on the injected
  clock. Otherwise it scales the int-or-percent value by `numNodes` and rounds **up** (`:386-407`).

**Pure variant.** `NodePoolBudgetMap(ctx, clk, nodePools, numNodes, disrupting, reason)` (`helpers.go:350-367`) has no
metrics or events. The pod deletion-cost ranking uses it (`pkg/controllers/pod/deletioncost/ranking.go:72-73`). It is
the natural budget function for a Snapshot service.

**How each method consumes the map.** The map is consumed **in place**:

| Method | Behavior |
| --- | --- |
| Emptiness | Decrements per kept candidate (`emptiness.go:76-85`). |
| Multi | Decrements per disruptable candidate before the search (`multinodeconsolidation.go:63-75`). |
| Single | Only checks `== 0` (`singlenodeconsolidation.go:80-83`). |

Validation rebuilds a fresh map and decrements it per candidate (`validation.go:233-248`, `:275-292`).

### 3.8 Validation, including churn rejection

**The wait**

- Both validators block on `select { case <-ctx.Done(): ...; case <-clock.After(validationPeriod): }`, but only when
  `validationPeriod > 0` (Emptiness `validation.go:135-141`, Consolidation `:200-206`).
- On cancel, the Emptiness validator returns `errors.New("interrupted")` and the Consolidation validator returns
  `errors.New("context canceled")`. Neither is a `ValidationError`, so both propagate as hard errors.
- The struct comment at `:96-98` says the wait is skipped "for all but the first command". That is stale; there is no
  such skip.

**`EmptinessValidator`** (`validation.go:134-148`, `:222-253`) filters to a subset and does not re-simulate.

1. Re-run `GetCandidates` with `Emptiness.ShouldDisrupt` and the graceful class (`:224`), then `mapCandidates` by
   `Name()` (`:228`; `helpers.go:441-446`).
2. If **zero** candidates survive, increment `FailedValidationsTotal` by `len(candidates)` and return
   `ChurnValidationError` (`:229-231`). Losing only some candidates is not an error here.
3. Against a fresh budget map, drop nominated candidates and candidates whose budget is 0, incrementing
   `FailedValidationsTotal` once per drop. Decrement the budget for each kept candidate (`:238-248`).
4. If nothing remains, return `BudgetValidationError` (`:252`).

On success the command's candidates shrink to the survivors (`:146`). `PoolDisruptionCosts` is not recomputed.

**`ConsolidationValidator`** (single and multi) is all-or-nothing. `isValid` (`:199-220`) runs:

1. The wait.
2. `validateCandidates` (`:263-294`):
   - Use `GetCandidates` with consolidation's `ShouldDisrupt`.
   - **Churn rejection:** if `len(validated) != len(candidates)`, add `len(candidates)` to `FailedValidationsTotal` and
     return `ChurnValidationError("%d candidates are no longer valid")` (`:271-274`). A candidate fails here if it:
     - became empty, because `ShouldDisrupt` rejects empty nodes;
     - lost `Consolidatable`;
     - was nominated, or was marked do-not-disrupt;
     - gained a blocking PDB;
     - joined a `WhenEmpty` pool.
   - Against a fresh budget map, a nominated candidate gives `BudgetValidationError` (`:283-286`). A zero budget gives
     `BudgetValidationError` (`:287-290`). Each failure adds `len(candidates)` to the metric.
   - `NewCandidate` already rejects nominated nodes (`state/statenode.go:310`), so a nomination during the wait
     normally shows up as churn. The explicit check at `:283-286` only catches a nomination that lands between the
     `GetCandidates` call and the check itself.
3. `validateCommand` (`:297-356`):
   - No candidates gives a plain `ValidationError` (`:299-301`).
   - It re-simulates with `IsConsolidationSimulation` (`:302`). A simulation error, including
     `errCandidateDeleting`, is wrapped plainly, so it is **not** a `ValidationError` and becomes a hard error
     (`:303-305`).
   - Non-pending pods that fail to schedule give `SchedulingValidationError` (`:306-308`).
   - The new NodeClaim count must agree with the command's replacements (`:316-335`):
     - Valid: 0 new NodeClaims and 0 replacements.
     - Valid: exactly 1 new NodeClaim and at least 1 replacement, provided the command's replacement types are a
       **subset by name** of the new NodeClaim's types (`:348`).
     - Every other combination gives `SchedulingValidationError`.
   - The re-simulation's `Results` are discarded. Nominations later come from the original `cmd.Results`.
4. `validateCandidates` **again** on the validated set, to guard against the race in issue #1167 (`:214-218`).

**How callers handle a failure**

| Aspect | Single | Multi | Emptiness |
| --- | --- | --- | --- |
| Returned command | discarded (`_, err =`, `singlenodeconsolidation.go:103`) | assigned back (`multinodeconsolidation.go:101`) | used as the shrunk command |
| Rejection events | `ConsolidationRejected` per candidate (`disruption/events/events.go:193`) | none (empty command) | none; logs at V(1) (`emptiness.go:104`) |
| Next candidate tried | no | n/a | n/a |
| Marked consolidated | no | no | no |
| `FailedValidationsTotal` | `len(candidates)` per churn or budget failure; scheduling failures from `validateCommand` do not increment it | same as Single | once per dropped candidate, or `len` on total churn |

Tests avoid the wait in three ways:

- The suite controller uses `NopValidator` (`suite_test.go:152`).
- `TestConsolidationValidator` passes a delay of 0 (`validation_test.go:245`).
- Specs that use the real validator poll `Eventually(env.Clock.HasWaiters)` and then `env.Clock.Step(...)`. There are
  11 sites in `consolidation_test.go`, starting at `:3669`.

---

## 4. The Balanced policy and the Evaluator seam

### 4.1 API surface

- The policy type is `type ConsolidationPolicy string` (`pkg/apis/v1/nodepool.go:160`). Its values are `WhenEmpty`,
  `WhenEmptyOrUnderutilized` and `Balanced` (`:163-165`). The CRD enum admits all three, and the default is
  `WhenEmptyOrUnderutilized` (`:100-101`).
- `const BalancedK int32 = 2` (`:172`) is a constant, not a field. A move is approved when its score is at least
  1/k = 0.5 (`:168-171`). The predicate is `IsBalanced()` (`:175-177`).
- **No feature gate guards Balanced.** `FeatureGates` (`pkg/operator/options/options.go:61-74`) has no Balanced field.
  The RFC's rollout still says "`--feature-gates BalancedConsolidation=true` to opt in"
  (`designs/balanced-consolidation.md:453`).

### 4.2 Disruption cost per pod and per node (`pkg/utils/disruption/disruption.go`)

**Per pod: `EvictionCost(ctx, p)`** (`:97-131`)

1. Start at `cost := 1.0`.
2. If the pod has `karpenter.sh/disruption-cost` (`pkg/apis/v1/labels.go:64`), add `ParseInt(v,10,32) / 2^27`
   (`:99-110`).
3. Otherwise, **only when the `PodDeletionCostManagement` gate is off**, add `controller.kubernetes.io/pod-deletion-cost
   / 2^27` (`:111-123`).
4. Add `*Spec.Priority / 2^25` (`:124-128`).
5. Clamp to [-10, 10] (`:130`).

A value that fails to parse is logged and skipped.

**Per node**

| Function | Line | Definition |
| --- | --- | --- |
| `PerNodeBaseDisruptionCost` | `:37` | `1.0` |
| `ComputeRescheduleDisruptionCost(ctx, reschedulablePods)` | `:51-57` | `1.0 + Σ max(0, EvictionCost(p))` |
| `SavingsRatio(price, rescheduleDisruptionCost)` | `:61-66` | `price / rescheduleDisruptionCost`, and panics on 0 |
| `LifetimeRemaining(clock, nodePool, nodeClaim)` | `:71-80` | The fraction of ExpireAfter left, clamped to [0, 1], or 1 if unset |
| `ReschedulingCost(ctx, pods)` | `:133-139` | An unclamped Σ `EvictionCost`. In package `disruption` only the legacy `Candidate.DisruptionCost` uses it (`types.go:224`); Balanced does not. Static deprovisioning also calls it (`pkg/controllers/static/deprovisioning/controller.go:302-303`). |

**Non-candidate nodes** use `StateNode.DisruptionCost() = 1.0 + Σ podDisruptionCosts` (`state/statenode.go:528-534`).
That map is kept incrementally:

- `updateForPod` stores `EvictionCost(pod)` for non-DaemonSet pods only when it is greater than 0, and otherwise
  deletes the key (`:575-586`).
- `cleanupForPod` deletes the key (`:599`).

**Two traps for a Snapshot service**

1. **Different pod sets.** The candidate path counts `IsReschedulable` pods (`types.go:213`), which excludes DaemonSet,
   mirror and inactive pods but keeps terminating StatefulSet pods (`pkg/utils/pod/scheduling.go:45-52`). The StateNode
   path excludes only DaemonSet pods.
2. **The map is not carried forward.** `newStateFromNodeClaim` drops `podDisruptionCosts` (`state/cluster.go:747-760`,
   section 1.4). A non-candidate node's cost can read as 1.0 right after a NodeClaim update.

To reproduce upstream totals, read both values exactly where upstream reads them (`balanced.go:82-86`). Do not
recompute them from pods.

### 4.3 How totals are computed

`computeNodePoolTotals(_ context.Context, allCandidates []*Candidate, allNodes []*state.StateNode, clusterCost *cost.ClusterCost) map[string]NodePoolTotals`
(`balanced.go:48-103`) is called from `GetCandidatesWithTotals` on **every** candidate `NewCandidate` accepted, before
`ShouldDisrupt` (`helpers.go:296-297`).

1. **Pass 1, over candidates** (`:61-69`). For each pool, keep a `*v1.NodePool` and `totalCostFallback += c.Price`.
2. **Pass 2, over all nodes with a `karpenter.sh/nodepool` label** (`:74-87`). Add the candidate's
   `RescheduleDisruptionCost` if the node is a candidate, and `n.DisruptionCost()` otherwise. Non-candidates are nodes
   `NewCandidate` rejected, such as do-not-disrupt, PDB-blocked, nominated or uninitialized nodes.
3. **Pass 3, totals** (`:89-101`). Only pools with at least one candidate get an entry. `TotalCost` is
   `clusterCost.GetNodepoolCost(np)` when that is greater than 0, and the fallback otherwise.
   - `GetNodepoolCost` reads the live `ClusterCost` under its read lock (`pkg/state/cost/cost.go:354-362`).
   - That read is a point-in-time value, outside the deep-copied node set.
   - `GetCandidates` passes a nil `clusterCost` (`helpers.go:265`), so validation-time totals would use the fallback.
     They are discarded anyway.

Totals are recomputed on every `disrupt()` call, that is, once per method per pass. Within a pass they are a snapshot,
as the RFC allows (`designs/balanced-consolidation.md:113`, `:181`).

### 4.4 Score math

```text
savings_fraction    = savings / TotalCost(pool)
disruption_fraction = disruption / TotalDisruptionCost(pool)
score               = savings_fraction / disruption_fraction
approved            = score >= 1/k        (k = BalancedK = 2, so 0.5)
```

**`ScoreMove(savings, disruptionCost, totals, k)`** (`balanced.go:109-122`)

- If `TotalCost <= 0 || TotalDisruptionCost <= 0`, it returns `ScoreResult{K: k}`, which scores 0 and is **rejected**.
- `Score()` returns 0 when the savings fraction is at most 0 and `+Inf` when the disruption fraction is 0
  (`types.go:118-126`).
- `Threshold()` is `1/K`, and `Approved()` is `Score() >= Threshold()` (`types.go:128-129`).

**`EvaluateBalancedMove(ctx, cmd, totals)`** (`balanced.go:131-183`)

1. With zero candidates, return `(false, nil)`.
2. Group the candidates by pool with `lo.GroupBy`, a map, so iteration order varies. Compute
   `savings := cmd.EstimatedSavings()` once.
3. Skip non-Balanced pools (`:144-146`).
4. For each Balanced pool:
   - `disruptionCost := cmd.PoolDisruptionCost(pool)`, which reads `PoolDisruptionCosts` and falls back to summing the
     candidates only when the map is nil (`types.go:317-329`). `computePoolDisruptionCosts` fills that map at every
     consolidation and emptiness construction site (`consolidation.go:185`, `:249`, `:299`, `:338`; `emptiness.go:99`);
     drift, static drift and repair fill it too.
   - **Cross-pool attribution:** `poolSavings = savings * poolCost / SourceCost()`, but only when `len(byPool) > 1` and
     `SourceCost() > 0` (`:153-158`). `byPool` counts non-Balanced pools too.
   - Call `ScoreMove(poolSavings, disruptionCost, totals[pool], BalancedK)`.
   - Log every input at V(1) as "consolidation score" (`:163-177`).
5. Approval is the AND over Balanced pools. A pool missing from the totals map has zero totals and is rejected.
6. `perPool` is always a non-nil map (`:140`), even when no pool is Balanced.
7. `ComputeMoveDisruptionCost` (`:125-127`) has no non-test callers.

**Worked example**

- A Balanced pool has `TotalCost = $10/h` and `TotalDisruptionCost = 50`.
- Deleting a $1/h node whose `RescheduleDisruptionCost` is 6 gives a savings fraction of 0.10 and a disruption
  fraction of 0.12.
- The score is 0.83, which is at least 0.5, so the move is approved.
- If the same node cost $0.30/h, the savings fraction would be 0.03 and the score 0.25, so the move would be rejected.

### 4.5 The Evaluator seam

**`noopEvaluator`** (`balanced.go:198-205`)

- `ApproveCommand` returns `(true, nil)`.
- `CanPassThreshold` returns true.
- `EmitMultiNodeEvents` does nothing.

It is the construction default. In production, `SetNodePoolTotals` always replaces it before `ComputeCommands`, so in
practice it only appears in tests that call `ComputeCommands` directly.

**`balancedEvaluator{totals, recorder}`** (`balanced.go:209-217`)

- **`ApproveCommand`** (`:221-251`) calls `EvaluateBalancedMove`. **Only when `len(cmd.Candidates) == 1`** does it
  also:
  - increment `ConsolidationMovesTotal{decision, nodepool, policy}` for **every** policy. The decision is "rejected"
    only when the pool was scored and not approved;
  - observe `ConsolidationScoreHistogram` for scored pools;
  - publish `ConsolidationApproved` on approval (`disruption/events/events.go:219`), one event each on the Node and
    the NodeClaim.

  Rejections produce metrics only, no event (`balanced.go:219-220`).
- **`EmitMultiNodeEvents`** (`:253-284`) runs per pool in the command:
  - It increments `ConsolidationMovesTotal` with the command-level decision, for all policies.
  - For scored pools it observes the histogram with the per-pool decision.
  - For scored pools that approve, it publishes `ConsolidationApprovedMultiNode`, one event on the NodePool
    (`disruption/events/events.go:239-248`).
- **`CanPassThreshold`** (`:288-300`) returns true for non-Balanced pools, for a missing totals entry, and for
  `TotalCost <= 0`. Otherwise it returns `ScoreMove(c.Price, c.RescheduleDisruptionCost, totals, BalancedK).Approved()`,
  the DELETE upper bound. **Missing or zero totals pass this check but fail `ApproveCommand`.**

**Call sites and the order of operations**

| Path | Order |
| --- | --- |
| Single (`singlenodeconsolidation.go:65-112`) | budget, then `CanPassThreshold`, then simulate, then `ApproveCommand` (perPool discarded), then validate |
| Multi (`multinodeconsolidation.go:115-189`) | No `CanPassThreshold` pre-filter (only the budget filter, `:63-75`). In each search step: simulate, then `filterOutSameInstanceType`, then `ApproveCommand`; a rejection shrinks the window. Then `EmitMultiNodeEvents(true)` before validation when `perPoolResults` is non-nil (`:96-99`), or `EmitMultiNodeEvents(false)` once when the search found nothing and some step was rejected by the evaluator (`:185-187`). |
| Emptiness, Repair | Receive totals but never call the evaluator. |

Consequences:

- Metrics and the approved event fire **before** validation, and validation never re-scores (`validation.go:192-220`).
- Multi-node windows always hold at least 2 candidates, so the single-candidate metric branch never fires there.
- Under `balancedEvaluator`, `perPoolResults` is non-nil even for commands where every pool uses
  `WhenEmptyOrUnderutilized`, so `moves_total` counts those moves too.

**Metric and event details**

- `karpenter_consolidation_score` (`metrics.go:243-253`) has buckets `{0.1, 0.25, 0.33, 0.5, 1, 2, 5, 10}`.
- `karpenter_consolidation_moves_total` (`:254-263`).
- Both have no subsystem. Their labels are `decision` (`ApprovalDim`: approved or rejected, `:88-101`), `nodepool` and
  `policy`.
- `policy` carries the raw `ConsolidationPolicy` string, such as `Balanced` (`balanced.go:229`, `:263`). That differs
  from the snake-cased `consolidation_policy` label (`pkg/metrics/labels.go:145-163`).
- The approval message is always the `>=` form `"score %.2f >= threshold %.2f (k: %d, savings %.1f%%, disruption %.1f%%)"`
  (`disruption/events/events.go:214-217`). The reasons are defined at `pkg/events/reason.go:28-29`.

### 4.6 Where the RFC and the code disagree

| Topic | RFC (`designs/balanced-consolidation.md`) | Code |
| --- | --- | --- |
| Feature gate | `BalancedConsolidation` gate (`:453`) | none |
| Rejection events | `ConsolidationRejected` is emitted for score rejections (`:193-196`) | metrics only (`balanced.go:219-220`); `ConsolidationRejected` is used only for validation failures |
| Base cost per node | `sum(max(0, EvictionCost))`, no base (`:119`) | +1.0 per source node (`disruption.go:51-57`) |
| Which pods count | all pods on the source nodes (`:127`) | reschedulable pods only (`types.go:213`, `:226`) |
| Separate annotation | rejected (`:37-39`) | `karpenter.sh/disruption-cost` is read first (`disruption.go:99`) |
| Integer k in the API | `consolidationPolicy: 3` (`:361`), though the RFC also defers integer tunability (`:367`) | not implemented (`nodepool.go:101`, `:172`) |

The RFC's open questions are at `:406-416`:

- whether k=2 works across workloads (`:408`);
- single-node pools scoring 1.0 (`:410`);
- adoption of pod deletion cost (`:412`);
- move quality (`:414`);
- **async move generation**: "generate, score, enqueue by priority, execute as budget allows. This eliminates the
  per-cycle timeout problem" (`:416`). That is this pipeline.

### 4.7 Tests that pin Balanced behavior

| File | What it pins |
| --- | --- |
| `disruption/balanced_test.go` | `ScoreMove` RFC examples (`:37-148`), zero and +Inf edges (`:150-172`), k = 1, 2 and 3 (`:174-190`), cross-pool equivalence (`:191`), `IsBalanced` (`:206-218`). |
| `disruption/balanced_scoring_test.go` (package `disruption`) | Totals over all candidates (`:185-230`), `EvaluateBalancedMove` edges and attribution (`:232-407`), a `ScoreMove` table (`:454-471`), on-demand-only savings (`:473`), `sortCandidates` ratio order (`:562-640`), multi-node events (`:641-698`). Helpers: `makeCandidate` (`:68`), `candidateNodes` (`:127`), `mockRecorder` (`:137`). |
| `disruption/balanced_integration_test.go` | Single-node approve and reject, multi-node approve, 0% budget, empty-before-non-empty, delete vs replace, ratio sort in multi-node, mixed policies (`:55-811`). |
| `disruption/balanced_adversarial_test.go` | NaN price, `perPool` entries for every Balanced pool in a cross-pool command, nil instanceType, source pricing, single-node pool DELETE = 1.0 (`:34-205`). |

---

## 5. Time limits

| Limit | Constant (file:line) | Value | Kind | Clock | Where checked | On expiry |
| --- | --- | --- | --- | --- | --- | --- |
| Multi-node search | `MultiNodeConsolidationTimeoutDuration` (`multinodeconsolidation.go:34`) | 1 min | `const` | **wall** (`context.WithTimeout`, `:132`) | Before each scheduling attempt: `trySchedule` checks `ctx.Err()` (`scheduler.go:562-563`), `Solve` breaks on `DeadlineExceeded` (`:516-518`) and returns `ctx.Err()` (`:557`) | `ConsolidationTimeoutsTotal{multi}`; returns the last saved command or nothing (`:142-150`); a no-op result marks consolidated unless a budget filtered a candidate (`:86-92`) |
| Single-node scan | `SingleNodeConsolidationTimeoutDuration` (`singlenodeconsolidation.go:33`) | 3 min | `var` | injected (`:60`, `:66`) | Before each candidate, never during a simulation | `ConsolidationTimeoutsTotal{single}`; saves the unvisited pools to `PreviouslyUnseenNodePools`; no command; not marked consolidated (`:66-73`) |
| Validation wait | `commandValidationDelay` (`consolidation.go:45`) | 15 s | `const` | injected `clock.After` (`validation.go:139`, `:204`) | Once per validated command | Proceeds to re-validation |
| Queue command timeout | `minRetryDuration` / `maxRetryDuration` / `retryDurationScale` (`queue.go:66-67`, `:69`), combined by `GetMaxRetryDuration` as 80 ms x queued commands clamped to [10 min, 1 h] (`:88-94`) | 10 min to 1 h | `const` | injected (`queue.go:193`) | Every queue reconcile | Unrecoverable; rollback (section 1.2) |
| Consolidated-state expiry | literal `time.Minute*5` (`state/cluster.go:655`) | 5 min | literal | injected | `ConsolidationState()` | `MarkUnconsolidated()` |
| Abnormal-run log | `AbnormalTimeLimit` (`controller.go:260`) | 15 min | local `const` | injected | Each reconcile | V(1) log only |
| Nomination window | `nominationWindow` (`state/statenode.go:602-605`) | max(2 x BatchMaxDuration, 10 s) | func | injected | `Nominated(clk)` | Nomination lapses |
| Drain settle | `MinDrainTime` (`pkg/controllers/node/termination/controller.go:62`) | 5 s | `const` | injected (`:220`) | `awaitDrain` (`:198`) | `Drained=True` (`:226`) |
| Provisioner batch solve | literal `time.Minute` (`provisioner.go:445`) | 1 min | literal | wall | `Provisioner.Schedule` only | Not on the disruption path |

**How often existing tests hit them.** We ran the full `pkg/controllers/disruption` suite (453 specs, no `-race`) with
a probe. The probe was a scratch test file added through `go test -overlay`; no repository file was changed. It read
`karpenter_voluntary_disruption_consolidation_timeouts_total` after every spec.

- **One spec in 453 hits a limit.** `singlenodeconsolidation_test.go:193-210` sets
  `SingleNodeConsolidationTimeoutDuration = -5 * time.Second` (`:194`), sees `single` go up by 1, and checks
  `PreviouslyUnseenNodePools`. `AfterEach` restores 3 minutes (`:98`).
- **The multi-node timeout is never exercised.**
  - The only multi-node timeout spec is `"should stop multi-node consolidation after context deadline is reached"`
    (`disruption/suite_test.go:2305-2360`). It passes a context that has already expired (`:2354`) and asserts only
    that no node was tainted (`:2359`).
  - The probe saw the `multi` counter stay at 0 during this spec, so the pass ends before the binary search runs. The
    likely stop is the sync gate: `cluster.Reset()` clears `hasSynced` (`state/cluster.go:680`), so `Synced` lists
    with the expired context (`:203-213`), fails, and the pass requeues without error (`controller.go:152-154`).
  - The spec passes without reaching `multinodeconsolidation.go:142-150`.
- **No other spec reaches either limit.** The fake clock never advances inside one `ComputeCommands`, and no
  multi-node search comes near a minute of wall time. The slowest specs (a drift-budget spec and a consolidation
  parallelization spec) took about 12 s each.
- **No test asserts `ConsolidationTimeoutsTotal`.** A parity or conformance test that needs the timeout path must
  build it itself, for example by cancelling the context from a scheduler hook or by giving the adapter an injected
  deadline.

Other clock use in the suites:

- `env.Clock.Step` call counts per file: `consolidation_test.go` 14, `emptiness_test.go` 1, `drift_test.go` 3,
  `queue_test.go` 2, `repair_test.go` 61, `repair_terminatefirst_test.go` 19. In `consolidation_test.go`, 11 are
  31 s steps that release the real validator's 15 s wait; the other 3 are 2-3 minute steps past a grace period or
  `consolidateAfter` (`:2947`, `:3135`, `:3263`).
- The real validator's 15 s wait is driven at those 11 sites in `consolidation_test.go` (`Eventually(env.Clock.HasWaiters)`
  then `Step(31 * time.Second)`, for example `:3669-3677`), in specs that install `NewMethodsWithRealValidator`
  (`:3589`, `:4208`, `:4280`, `:4343`). The suite controller otherwise bypasses it (section 3.8).

**Consequences**

- The multi-node limit is the only decision-relevant timeout that depends on machine speed. At 1,000 nodes the spike
  measured about 0.7 s per multi-node `ComputeCommands` on 4 vCPUs (`experiments/RESEARCH_LOG.md`), so it does not
  fire at that scale with the in-memory backend. Parity runs must still stay well under it, or inject the deadline.
- The single-node limit fires only when the injected clock moves during a scan. A full scan at 1,000 nodes costs about
  58 s of wall time. That does not matter on a fake clock, but on a real clock it is a third of the 3-minute limit,
  and slower simulations or larger clusters reach the limit.

---

## 6. Sources of nondeterminism

"Parity" means one process runs the legacy methods and the pipeline adapters against the same frozen state and
compares their decisions. "Seeded runs" means the simulator or experiments must reproduce bit-for-bit across processes
given a seed.

| Source | path:line | Affects decisions? | Control for parity | Control for seeded runs |
| --- | --- | --- | --- | --- |
| `DeepCopyNodes` returns `lo.Values(map)` | `state/cluster.go:290` (`:63` map) | Yes, through tie order (next row) | Feed both paths the **same** candidate slice, built once, or sort by name first | Sort snapshot nodes and candidates by `ProviderID()`/`Name()` before any stage |
| `sortCandidates` is an unstable `sort.Slice` with no tie-breaker | `consolidation.go:149-154` | **Yes**: which prefix the multi-node search tries (`multinodeconsolidation.go:136`) and which candidate single-node returns first (`singlenodeconsolidation.go:111`) | Fixtures with distinct `SavingsRatio`, or pre-sort the input by name so the same input gives the same `pdqsort` output in both paths | Pipeline sorts by `(SavingsRatio desc, Name asc)`, a valid permutation of the legacy order |
| `shuffleCandidates` pool order | `singlenodeconsolidation.go:150-153` (`UnsortedList`, `lo.Keys`) | **Yes**, when there is more than one NodePool | One NodePool per parity case, or compare the pipeline against a legacy run whose pool order was recorded | Sort pool names; keep `PreviouslyUnseenNodePools` first, sorted |
| `PreviouslyUnseenNodePools` carried across passes | `singlenodeconsolidation.go:38`, `:70`, `:121` | Yes, it reorders the next pass | Start both paths with an empty set; replay the same timeouts | Part of method state; reset per run |
| Per-method `lastConsolidationState` | `consolidation.go:60`, `:90-97` | Yes, it skips whole passes | `cluster.MarkUnconsolidated()` before each case; give the pipeline its own two states (section 8.2) | Fake clock; state is per method copy |
| Multi-node wall-clock deadline | `multinodeconsolidation.go:132` | **Yes**, depends on machine speed | Keep cases far below 1 minute; assert the timeout counter did not move | Do not use it in planning code; use `SearchBudget` on the injected clock |
| `ConsolidationState` 5-minute auto reset | `state/cluster.go:655-661` | Yes, it re-enables passes | Fake clock; never step more than 5 minutes between compared calls without intending to | Fake clock |
| Validation wait | `validation.go:139`, `:204` | No, if state is frozen, because it only delays | `NopValidator` through `WithValidator`, or a delay of 0, or step the fake clock | Same |
| `fixedNowClock` per pass | `helpers.go:288` | No; it makes `DisruptionCost` consistent within a pass | Use the same injected clock | Same |
| `OrderByPrice` is an unstable `sort.Slice` | `cloudprovider/types.go:396-415`; callers `consolidation.go:210`, `cloudprovider/types.go:503`, and at launch `scheduling/nodeclaimtemplate.go:114` | Yes on price ties: the spot-to-spot `[0:15]` cut (`consolidation.go:329`, `:331`), the 600-type truncation, and `EstimatedSavings` reading `InstanceTypeOptions[0]` (`types.go:425`), which feeds the Balanced score (`balanced.go:137`) | Identical input order to both paths makes the output identical; compare instance-type **sets** | Use catalogs without exact price ties in decisive positions, or stable-sort by name first in pipeline-native code |
| `buildDaemonOverheadGroups` returns `lo.Values(map)` | `scheduler.go:1050` | Yes, it can permute `InstanceTypeOptions` (`nodeclaim.go:611-648`), and so interacts with the row above | Compare option sets | Avoid DaemonSets that split instance types into several overhead groups, or compare sets |
| New-claim sort by pod count | `scheduler.go:648` | Rarely; claims with equal counts | Same input gives the same output | Same |
| Topology-spread minimum domain picked by ranging over a map | `scheduling/topologygroup.go:274-289`; `nodeDomains.Values()` is `UnsortedList` (`:259`; `pkg/scheduling/requirement.go:297-299`) | **Yes** on ties: the zone or hostname requirement of a replacement, which then affects price filtering | Avoid spread ties in fixtures, or compare modulo zone | Same; documented as nondeterministic at `pkg/test/schedulingproperties/schedulingproperties.go:151-152` |
| `getMatchingTopologies` ranges over maps | `scheduling/topology.go:567-580` | Only which error is reported | Do not diff error text | Same |
| Existing-node order in the scheduler | `scheduler.go:890-903` (stable, by initialized then `Name()`) | Deterministic **given names**. Random test names (`pkg/test/metadata.go:37-45`) change first-fit order across processes | Same names in both paths, automatically | Pin object names in the generator |
| Pod queue order | `scheduling/queue.go:73-108` | Deterministic given UIDs and creation timestamps; UIDs are random per process | Automatic within one run | Assign UIDs deterministically from the seed |
| `Requirement.Any()` on `In`: `UnsortedList()[0]`; on `Exists`/`NotIn`: global `math/rand` | `pkg/scheduling/requirement.go:271-285` | On multi-valued requirements upstream uses it only for execution-time labels (`nodeclaimtemplate.go:102`) and in the fake provider's `Create` (`fake/cloudprovider.go:199`). `Offering.CapacityType()`, `Zone()` and `ReservationID()` also call it (`cloudprovider/types.go:596-606`), but on single-valued offering requirements, where it is deterministic | Never use it to derive a value that is compared, such as a LaunchSpec zone | Same; pick the minimum value instead |
| `hostname-placeholder-%04d` global counter | `scheduling/nodeclaim.go:87`, `:98` (bumped per template trial, `scheduler.go:773`) | No; stripped at finalize | Never compare hostnames | Same |
| Float summation order of totals | `balanced.go:61-87` (map-order candidates and nodes) | Only last-bit differences at exactly 0.5 | Use the same totals map in both paths (compute once) | Sort before summing in pipeline-native code |
| `EvaluateBalancedMove`, `EmitMultiNodeEvents` iterate `lo.GroupBy` maps | `balanced.go:136-180`, `:254-283` | No (the result is an AND); event and log order only | Compare events as multisets | Same |
| `NonPendingPodSchedulingErrors` prints the first 5 errors in map order | `scheduler.go:434-454` | No; event text only | Do not diff Unconsolidatable text | Same |
| `cmd.ID = uuid.New()`, `CreationTimestamp` | `controller.go:238-239` | No | Exclude from comparison | Derive IDs from the seed in the simulator |
| Scheduler UUID | `scheduler.go:200` | No; metric label | Ignore | Ignore |
| NodeClaim `GenerateName`, fake `RandomProviderID` | `nodeclaimtemplate.go:157`; `pkg/cloudprovider/fake/cloudprovider.go:210`; `pkg/test/metadata.go:79-80` | Execution only, but later passes key on these names | Not reached by decision-only parity | Override in the simulator's cloud provider |
| `workqueue.ParallelizeUntil` in hand-off, taints and deletes | `controller.go:234`; `queue.go:235`, `:311` | No; API call order only | Ignore | Ignore |
| Fake provider `Create` mutates the shared `ReservationCapacity` | `fake/cloudprovider.go:188-189` | Yes, across later passes when reserved offerings exist | Reset the provider between legacy and pipeline runs, or avoid reserved offerings | Rebuild instance types per run |
| `PriceFromResources` ranges over a map | `fake/instancetype.go:421-433` | Floating-point last bits with three or more priced resources (GPUs) | Same instance types object for both paths | Avoid GPU types in seeded catalogs, or round prices |
| Process-global price provider | `cloudprovider/types.go:897-915` (`SetNodeClaimPriceProvider`) | Yes, it sets every `Candidate.Price` | Never change it inside a test binary | Same |
| Live reads inside `SimulateScheduling` | `helpers.go:83`, `:101`, `:113`; `provisioner.go:289-331`, `:350`, `:354`; `scheduler.go:238`; `topology.go:315` | Yes, if state changes between the two paths' calls | Freeze the fake client and cluster state between paths; reset `Synced` and `MarkUnconsolidated` deliberately | Simulator controls every write |

**The parity test therefore needs:**

- the same candidate slice for both paths, or a name pre-sort;
- distinct ratios, or one NodePool;
- a fake clock and `NopValidator`;
- cases far below the 1-minute deadline;
- decisions compared as {candidate name set, decision, replacement instance-type name set, replacement NodePool},
  never names, hostnames or IDs.

---

## 7. Measurement assets inventory

### 7.1 Disruption metrics (`disruption/metrics.go`)

- The namespace is `karpenter` (`pkg/metrics/constants.go:27`), and the subsystem is `voluntary_disruption`
  (`disruption/metrics.go:28`).
- Label names are `decision`, `consolidation_type`, `policy` and `terminate_first_reason` (`disruption/metrics.go:29-30`,
  `:32-33`; `:31` is the `candidates_ineligible` constant, not a label), plus `reason` and `nodepool`
  (`pkg/metrics/labels.go:75-76`).

| Var (line) | Full name | Type, stability | Labels |
| --- | --- | --- | --- |
| `EvaluationDurationSeconds` (132) | `karpenter_voluntary_disruption_decision_evaluation_duration_seconds` | histogram, Beta | reason, consolidation_type |
| `DecisionsPerformedTotal` (144) | `..._decisions_total` | counter, GA | decision, reason, consolidation_type |
| `NodepoolDecisionsPerformed` (155) | `..._decisions_by_nodepool_total` | counter, Alpha | nodepool, decision, reason, consolidation_type |
| `TerminateFirstDecisionsTotal` (166) | `..._terminate_first_decisions_total` | counter, Alpha | nodepool, reason, terminate_first_reason |
| `EligibleNodes` (177) | `..._eligible_nodes` | gauge, Beta | reason |
| `ConsolidationTimeoutsTotal` (188) | `..._consolidation_timeouts_total` | counter, Beta | consolidation_type |
| `FailedValidationsTotal` (199) | `..._failed_validations_total` | counter, Alpha | consolidation_type |
| `NodePoolAllowedDisruptions` (210) | `karpenter_nodepools_allowed_disruptions` | gauge, GA | nodepool, reason |
| `NodePoolNodesConsumingBudgets` (221) | `karpenter_nodepools_nodes_consuming_budgets` | gauge, Alpha | nodepool, reason |
| `DisruptionQueueFailuresTotal` (232) | `..._queue_failures_total` | counter, Beta | decision, reason, consolidation_type |
| `ConsolidationScoreHistogram` (243) | `karpenter_consolidation_score` (no subsystem) | histogram, Alpha | decision (approved/rejected), nodepool, policy |
| `ConsolidationMovesTotal` (254) | `karpenter_consolidation_moves_total` (no subsystem) | counter, Alpha | decision (approved/rejected), nodepool, policy |
| `NodeClaimsUnhealthyDisruptedTotal` (267) | `karpenter_nodeclaims_unhealthy_disrupted_total` | counter, Alpha | condition, nodepool, capacity_type, image_id, termination_mode |

**The `consolidation_type` label**

- It documents exactly `multi`, `single` and `empty` (`metrics.go:37-56`).
- The `Values` list is documentation only: operatorpkg does not reject unlisted values
  (`github.com/awslabs/operatorpkg/metrics/labels.go:20-29`).
- `init()` pre-creates zero-valued timeout series for multi and single only (`:122-129`).
- `disrupt()` labels with the method's `ConsolidationType()`, so a pipeline method needs a documented `pipeline` value
  (section 8.1).

**Suite resets.**

- The suite-wide `AfterEach` resets `DecisionsPerformedTotal`, `NodepoolDecisionsPerformed`,
  `TerminateFirstDecisionsTotal` and `NodeClaimsUnhealthyDisruptedTotal` (`suite_test.go:196-204`).
- Individual `BeforeEach` blocks reset `FailedValidationsTotal` (`consolidation_test.go:180`), `EligibleNodes`
  (`emptiness_test.go:83`) and `DisruptionQueueFailuresTotal` (`queue_test.go:239`).
- Nothing resets `ConsolidationTimeoutsTotal`, `ConsolidationMovesTotal` or `ConsolidationScoreHistogram`. Assert
  deltas on those, not absolute values.

### 7.2 End-to-end latency harness

`test/pkg/environment/common/latency_harness.go` provides:

- `func StartLatencyHarness(env *Environment) (*LatencyHarness, error)` (`:79`)
- `func (h *LatencyHarness) Stop() (*LatencyResult, error)` (`:95`)

It scrapes eight histograms (`TargetHistograms`, `:47-56`) and four counters (`TargetCounters`, `:58-63`). The
consolidation-relevant ones are the histograms `karpenter_consolidation_score` (`:55`) and
`karpenter_voluntary_disruption_decision_evaluation_duration_seconds` (`:53`), and the counters
`karpenter_voluntary_disruption_consolidation_timeouts_total` (`:59`) and `karpenter_consolidation_moves_total`
(`:60`). The others cover pod scheduling and binding, scheduler duration, NodeClaim termination and node and
NodeClaim creation.

### 7.3 Performance suite (`test/suites/performance`)

**Shared setup** (`suite_test.go:50-66`). Each spec gets a fresh NodePool with `ConsolidateAfter=30s`, a `100%`
budget, no limits and the policy `WhenEmptyOrUnderutilized`. On KWOK it also sets `instance-size Lt 32` (`:55-60`).

**Balanced specs**

- **Basic** (`balanced_basic_test.go:33-59`):
  1. Set the policy to Balanced.
  2. Create 500 x (900m, 3100Mi) and 500 x (3500m, 28Gi), then `ReportScaleOutWithOutput(...,1000,15m)`.
  3. Start the latency harness.
  4. Scale both groups to 350, then `ReportConsolidation(env,...,1000,700,nodes,20m)` (`:50`).
  5. Assert with `expectBalancedDecisionsMatchThreshold` (`:58`).
- **Churn** (`balanced_churn_test.go:44-85`):
  1. Create 400 x (900m, 3100Mi).
  2. Run three `scaleAndSettle` rounds: 200, then 400, then 200 (`:66-71`). `scaleAndSettle` (`:34-40`) waits for
     healthy pods, then sleeps 2 x `ConsolidateAfter`.
  3. `ReportConsolidation(...,400,200,...,20m)` (`:74-76`), then `expectBalancedDecisionsMatchThreshold` (`:84`).
- **Heterogeneous** (`balanced_heterogeneous_test.go:53-106`), KWOK only (`:49-51`):
  1. Build two NodePools restricted to families `c` and `m` (`buildFamilyRestrictedNodePool`, `:35-44`).
  2. Create 300 x (500m, 1Gi) on `c` and 100 x (2500m, 8Gi) on `m`.
  3. Scale to 180 and 60, then `ReportConsolidation(...,400,240,...,25m)` (`:90-92`).
  4. Assert that the scored pools are a subset of {c, m} (`:103-105`).

**`expectBalancedDecisionsMatchThreshold`** (`balanced.go:32-54`). For Balanced-policy series of
`karpenter_consolidation_score`:

- approved series must have a minimum of at least 0.33, the bucket just below 0.5 (`:30`);
- rejected series must have a maximum of at most 1/k;
- at least one move must have been scored.

**Reporting**

- `func ReportConsolidation(env *common.Environment, testName string, initialPods, finalPods, initialNodes int, timeout time.Duration) (*PerformanceReport, error)`
  (`report.go:192`) runs these steps:
  1. Start the profiler and metrics poller, and record a baseline of `karpenter_pods_drained_total` (`:193-198`; the
     counter is read by `getPodsDisruptedCount`, `:36-46`).
  2. Wait for `finalPods` healthy pods.
  3. Run `monitorConsolidationRounds` (`:207`).
  4. Build a `PerformanceReport` (`types.go:36`): nodes, average utilization, `ResourceEfficiencyScore = cpu*90 + mem*10`,
     pods disrupted, rounds, and heap and CPU P95/avg/max.
- `monitorConsolidationRounds` (`report.go:378`) polls every 30 s (`:431`).
  - A "round" is a poll that sees a node with a DeletionTimestamp or a `karpenter.sh/disrupted` taint (`:390-402`). It
    then sleeps 25 s (`:409`) and records the node delta (`:410-422`).
  - It stops after 3 minutes without draining (`:425-427`) or at the timeout.
  - It uses the wall clock and is coarse.
- `ReportConsolidationWithOutput` (`:456`) fails if no metrics were sampled. The Balanced specs call plain
  `ReportConsolidation` and then `OutputPerformanceReport` for the consolidation phase, so that check does not apply
  there (it does apply to their `ReportScaleOutWithOutput` phase, `:442-453`). `OutputPerformanceReport` (`:50-80`)
  calls `writeReportFiles` (`:85-105`) only when `OUTPUT_DIR` is set; it writes `<prefix>_performance_report.json`
  and pprof files there.

**Thresholds** (`thresholds.go`)

- Per-test overrides come from the `KARPENTER_PERF_THRESHOLDS` JSON, keyed `"<testCase>/<phase>"` (`:28-33`).
- Parsing is strict: unknown fields fail (`:54-68`), and a parse error panics (`:73-80`).
- The five accessors (heap, CPU, total time, CPU and memory utilization) are at `:82-115`.
- The three Balanced specs set no thresholds.

### 7.4 Seeded scenario generators (`pkg/test/schedulingproperties/schedulingproperties.go`)

- **Public API.** Three property checks, each `func(ctx context.Context, t TB, cfg Config)`:
  - `ExpectSatisfiablePodsArePlaced` (`:104`)
  - `ExpectPlacementsFitNodes` (`:133`)
  - `ExpectEquivalentPodsScheduleAlike` (`:146`)

  The config is `Config` (`:79-99`): `CloudProvider`, `InstanceTypes` and `Launch` are required, and `NodePool`,
  `RestrictedLabelDomains`, `KnownDeviations` and `Checks` are optional. Example call:
  `pkg/controllers/provisioning/scheduling/scheduling_properties_test.go:31-56`.
- **Generators are unexported.** `scenario` (`:179-188`) and `drawScenario(t *caseT, cfg Config, satisfiable bool)`
  (`:192`) draw:
  - 1 to 20 instance types (`:193`), NodePool requirements (`drawNodePoolRequirements`, `:464`) and minValues
    (`drawMinValues`, `:272`);
  - 1 to 5 pods (`:251`) with node affinity, pod affinity, anti-affinity, spread, host ports and volumes (`drawPod`,
    `:516`; `drawVolume`, `:395`).
- **Solving.** `solve` (`:635-661`) builds a controller-runtime fake client (`:640-649`), then
  `state.NewCluster(clock.RealClock{}, ...)` and `provisioning.NewProvisioner` (`:651-652`), and calls `prov.Schedule`
  (`:656`). It covers provisioning only and has no consolidation path.
- **Seeding.**
  - The flags are `-schedulingproperties.seed`, which defaults to 1 (0 means time-based), and
    `-schedulingproperties.checks` (`:1086-1087`). The seed is chosen at `:1137`.
  - gopter, seeded with that seed (`:1138`), draws each case's `l.Seed` (`:1148`), and each case gets
    `rand.New(rand.NewSource(l.Seed))` (`:1174`). Shrinking lowers the limits and keeps the seed (`shrink`, `:1179`).
  - The default limits are `{InstanceTypes:20, Pods:5, RequiredTerms:3, PreferredTerms:2}` (`:1105`).
- **Reuse for disruption** means exporting `drawScenario` or copying it.

### 7.5 Instance catalogs

**`kwok/examples/aws_instance_types.json`** (9.5 MB)

- 1724 entries: 853 names x {linux, windows}, with 18 duplicate (name, OS) pairs from i386 and x86_64 variants.
- Each entry has `{name, offerings:[{Price, Available, Requirements}], architecture, operatingSystems, resources}`.
- Each entry has 8 offerings: us-west-2a to d, x spot/on-demand. All are available, and the on-demand price is the
  same in every zone.
- 125 name prefixes before the `.`, which KWOK's family rule (below) collapses to 115 families (for example
  `c7i-flex` and the `u-*tb1` types merge). They include c7i, m7i and r7i with 11 Linux sizes each.
- Sample on-demand prices: c7i.large 0.09818, m7i.large 0.1008, r7i.large 0.14553.
- **Exact price ties exist** (c7i.48xlarge = c7i.metal-48xl = 8.568), and so does **non-monotone pricing**
  (m7i.metal-24xl 5.32224 > m7i.24xlarge 4.8384).

**How KWOK loads a catalog**

- The file is loaded through `--instance-types-file-path` / `INSTANCE_TYPES_FILE_PATH` (`kwok/options/options.go:42`)
  in `ConstructInstanceTypes` (`kwok/cloudprovider/helpers.go:70-92`).
- `setDefaultOptions` (`:125-155`) forces `Available=true` and appends an `on-demand-alt` offering at a 10% discount
  for each on-demand offering (`onDemandAltOfferings`, `:218-234`; `onDemandAltDiscount = 0.1`,
  `kwok/cloudprovider/const.go:27`).
- The family is the text before the first `.` or `-` (`:42`, `:113-123`), so `c7i-flex` has family `c7i`.
- The size comes from `awsRegexp` (`:40`, `:97-103`). It does not match `metal-24xl`, which falls back to the CPU
  count. Other AWS sizes are words such as `large`, so the perf suite's `instance-size Lt 32` excludes them.

**Other catalogs**

- `kwok/examples/instance_types.json` holds one entry in a stale schema that does not map onto
  `KWOKOffering.Requirements` (`helpers.go:48-51`).
- The default embedded catalog is `kwok/cloudprovider/instance_types.json` (`helpers.go:66-67`), generated by
  `Makefile:203`. It has 144 entries in families `c`, `m` and `s` (48 each). The perf and e2e suites use it.

**Unit tests** use `fake.InstanceTypesAssorted()` (`pkg/cloudprovider/fake/instancetype.go:364-398`): 1344 types
(7 CPU sizes x 8 memory sizes x 3 zones x 2 capacity types x 2 OSes x 2 architectures), with one offering each.

- The price is `0.1·cpu + 0.1·memGB` (+1 per GPU) and does not depend on capacity type or zone (`:421-433`), so ties
  are everywhere.
- 1344 is above the 600 cap (`MaxInstanceTypes`, `scheduling/nodeclaimtemplate.go:50`), so truncation does take effect.

### 7.6 `designs/scripts/balanced-consolidation-ranking.py`

**Setup**

- NumPy is seeded with `SEED = 42` (`:29`). The random-ranking baseline uses `SEED + 1` (`:326`, `:367`).
- Instance prices are hard-coded for 26 c7i, m7i and r7i on-demand types, labelled us-east-1 (`:36-66`). Only 12 of
  them equal the us-west-2 catalog's price (for example m7i.large 0.1008); most of the rest are about 10% lower (for
  example c7i.large 0.0893 against 0.09818).

**Simulation**

1. `build_cluster` (`:122`) packs pods first-fit-decreasing; a new node gets the cheapest fitting type, upsized to a
   type about 4x bigger when that costs less than 2x as much (`:126-131`). Its default is `n_pods=20000`, but `main`
   calls it with `n_pods=5000` (`:414-418`).
2. Ten rounds of `churn_pods` (`:179-208`, `:430-438`) run.
3. Two kinds of move are generated:
   - REPLACE moves: the cheapest type for the remaining pods (`:211-249`).
   - DELETE moves: best-fit scatter onto spare capacity (`:252-317`).
4. Each move is scored as `savings_frac / disruption_frac`, with per-node disruption = 1 + Σ pod cost (`:99-102`).

**Output.** Plots go to `designs/ranking-strategies.png` (`:453-456`), and CSVs to
`designs/scripts/ranking-strategies-{all,replace,delete}.csv` (`:459-469`). The docstring's usage line and output
paths (`:15-20`) are stale.

### 7.7 kind, KWOK and the scheduler configuration

- **`make setup-kind`** (`Makefile:70-73`) recreates a kind cluster with `--config hack/kind/cluster.yaml` and taints
  the control plane. It does not install KWOK.
- **`hack/kind/cluster.yaml`**:
  - mounts `test/suites/regression/testdata` at `/etc/kubernetes/karpenter-scheduler-config` (`:15-18`);
  - points kube-scheduler at `kube-scheduler-config.yaml`, for kubeadm v1beta3 and v1beta4 (`:23-47`);
  - uses a relative hostPath, so it must run from the repository root.
- **`test/suites/regression/testdata/kube-scheduler-config.yaml`** defines the profiles `default-scheduler` (`:15`)
  and `topology-spread-defaults` (`:16-34`). Only the second carries default spread constraints: hostname and zone,
  maxSkew 1, DoNotSchedule, applied only to pods that select it by `schedulerName`.
- **`make install-kwok`** (`Makefile:23-24`) runs `hack/install-kwok.sh` with `KWOK_RELEASE=v0.8.0` (`:17`). It
  applies `stage-fast.yaml` (`:166`, `:175`) and `hack/kwok/stages` (`:176`): node-heartbeat-with-lease,
  node-unhealthy, pod-delete and pod-unhealthy.
- **`make apply-with-kind`** (`Makefile:45-53`) runs verify, then `ko build`, then a helm install of `kwok/charts`.
  Feature gates are a hard-coded `FEATURE_GATES` string (`kwok/charts/templates/deployment.yaml:103-104`), with values
  at `kwok/charts/values.yaml:157-171` and overrides in `HELM_OPTS` (`Makefile:8-16`). **A new gate needs chart,
  values and `HELM_OPTS` changes.**
- **CI** (`.github/workflows/e2e.yaml`) creates the cluster with `helm/kind-action` and the same `cluster.yaml`
  (`:39-44`), then runs `install-kwok` and `apply-with-kind` (`:63-71`).
- **Network.** On this session VM, `registry.k8s.io` and `cgr.dev` are blocked, so KWOK runs are skipped here
  (`experiments/RESEARCH_LOG.md`).

### 7.8 Unit-test harness to reuse (`disruption/suite_test.go`)

- **BeforeSuite** (`:101-113`):
  - `test.NewEnvironment(test.WithCRDs(coreapis.CRDs...), test.WithCRDs(v1alpha1.CRDs...))`
  - `options.ToContext(ctx, test.Options())`
  - `fake.NewCloudProvider()`
  - `cost.NewClusterCost`
  - `informer.NewPricingController`
  - `test.NewEventRecorder()`
  - `deviceallocation.NewController`
  - `prediction.NewStore`
  - `useRepairPolicies`
- **`useRepairPolicies`** (`:117-126`):
  - builds `state.NewCluster(env.Clock, env.Client, cloudProvider, state.WithRepairPolicyMatcher(...))`;
  - builds the node and NodeClaim informer controllers;
  - builds `provisioning.NewProvisioner(env.Client, recorder, cloudProvider, cluster, env.Clock, draController, virtualpods.NewVirtualPodCache(env.Client), store)`;
  - rebuilds `disruption.NewQueue`.
- **BeforeEach** (`:132-194`) runs these steps:
  1. Reset the provider and set `cloudProvider.InstanceTypes = fake.InstanceTypesAssorted()`.
  2. Reset the cluster cost and the pricing reconcile.
  3. Reset the recorder, then call `useRepairPolicies` again (`:138-139`), which rebuilds cluster state, the
     informer controllers, the provisioner and the queue for every spec.
  4. Rebuild the DRA controller, the prediction store and the provisioner (`:144-149`).
  5. Build `disruption.NewController(..., disruption.WithMethods(NewMethodsWithNopValidator()...))` (`:152`).
     `NewMethodsWithNopValidator` (`validation_test.go:109-121`) returns StaticDrift, Drift, Emptiness, Multi and
     Single, with no Repair and in a different order from production's `NewMethods` (`controller.go:102-123`).
  6. Call `env.Clock.SetTime(time.Now())`, `cluster.Reset()` and `cluster.MarkUnconsolidated()`, and rebuild the queue
     (`:153-156`); then reset the feature gates with `options.ToContext(ctx, test.Options())` (`:159`).
  7. Build the cheapest and most-expensive fixtures (`:161-188`).
- **A direct `SimulateScheduling` call** (`:257-279`): `BuildNodePoolMap`, then `pdb.NewLimits`, then
  `ExpectStateNodeExists`, then `NewCandidate(..., GracefulDisruptionClass)`, then
  `SimulateScheduling(..., nil, SimulationOptions{}, candidate)`.
- **A direct method call** (`consolidation_test.go:316-325`): `MakeConsolidation`, then
  `NewSingleNodeConsolidation(c, WithValidator(...))`, then `BuildDisruptionBudgetMapping`, then
  `GetCandidates(..., m.ShouldDisrupt, m.Class(), queue)`, then `m.ComputeCommands`. This path never calls
  `SetNodePoolTotals`, so the method keeps the `noopEvaluator` and Balanced pools are not scored (section 4.5).
- **envtest fidelity trap.** kube-apiserver adds `node.kubernetes.io/not-ready:NoSchedule` to every created Node, and
  nothing removes it. Parity fixtures must strip it, as `ExpectMakeNodesReady` does (`experiments/RESEARCH_LOG.md`).

---

## 8. Proposed seams

### 8.1 Where `PipelineConsolidation` registers

**Constraint: import direction.**

- `NewMethods` is in package `disruption`. The pipeline's upstream adapters need `disruption.Candidate`, `Command`,
  `Method` and the consolidation methods, so `pipeline` imports `disruption`, as the working tree's
  `pipeline/upstream.go` already does.
- `disruption` therefore cannot import `pipeline`, and `NewMethods` cannot construct the pipeline method directly.

**Recommended registration: compose at the wiring layer** (`pkg/controllers/controllers.go:129`)

```go
// Illustrative. Neither function exists upstream; the working tree's pipeline/register.go implements
// ReplaceConsolidation and builds the method with pipeline.NewMethod (NewConsolidation is a placeholder name).
methods := disruption.NewMethods(ctx, clock, cluster, kubeClient, p, cloudProvider, recorder, disruptionQueue)
if options.FromContext(ctx).FeatureGates.ConsolidationPipeline {
	methods = pipeline.ReplaceConsolidation(methods, pipeline.NewConsolidation(...)) // drops *MultiNodeConsolidation and *SingleNodeConsolidation, inserts at the multi slot
}
disruption.NewController(ctx, clock, kubeClient, p, cloudProvider, recorder, cluster, disruptionQueue, clusterCost, disruption.WithMethods(methods...))
```

The working tree's `disruptionMethods` helper in `pkg/controllers/controllers.go` follows this pattern: it returns no
options when the gate is off and a single `WithMethods(pipeline.Methods(...)...)` otherwise, where `pipeline.Methods`
calls `disruption.NewMethods` and then `ReplaceConsolidation` (`pipeline/register.go`).

This works because of how the options compose:

- `NewController` resolves `[WithMethods(NewMethods(...)...)] ++ opts` in order (`controller.go:87`).
- `WithMethods` **assigns** the list (`:80`), so the caller's list wins outright.
- `NewMethods` is still evaluated once, eagerly, for the default. That is harmless, except that `NewRepair` panics
  without a matcher under the NodeRepair gate (`repair.go:63-67`). Production builds the matcher
  (`kwok/main.go:43-44`), so this is safe there. With the gate on, the working tree evaluates `NewMethods` twice per
  controller (once in `pipeline.Methods`, once as the default), which is equally harmless.

The replacement must keep the rest of the order: Repair, Emptiness, StaticDrift, Drift, **Pipeline**.

- Identify the consolidation methods by type (`*disruption.MultiNodeConsolidation`, `*disruption.SingleNodeConsolidation`),
  not by position, because Repair is conditional.
- Emptiness keeps its `Empty` reason and budgets.
- Drift and StaticDrift stay untouched.
- With the gate off, `controllers.go` effectively calls `NewController` with no options (the helper returns nil), so
  the upstream diff is one gated helper and its call site.

**Alternative: an injected factory in package `disruption`**, for example a `ControllerOptions` field
`consolidationFactory func(c consolidation) []Method`.

- This needs an exported way to pass the unexported `consolidation` type, which is a wider API change.
- Prefer it only if several entry points (tests, the simulator, the operator) must share the gate logic.
- Even then, the simulator and the parity test can call `WithMethods` themselves, as the suite does at
  `suite_test.go:152`.

**Gate plumbing**

1. Add `ConsolidationPipeline bool` to `FeatureGates` (`pkg/operator/options/options.go:61-74`).
2. Add a `ConsolidationPipelineFeatureGate = FeatureGate{Name, Default: false, Stage: docs.Alpha, Help}` var
   (`:89-160`).
3. Append it to `KarpenterFeatureGates` (`:163-174`), `DefaultFeatureGates` (`:289-302`) and `ParseFeatureGates`
   (`:305-`).
4. Add it to `pkg/test/options.go` (`:57`, `:100-`).
5. `pkg/operator/options/featuregates_test.go:30-57` checks that every field is documented, defaulted and parsed. It
   looks fields up by the gate's `Name` (`:42`, `:47`, `:54`), so the field must be named exactly `ConsolidationPipeline`.
6. For KWOK, add it to the chart and values (section 7.7).

**Method identity**

| Item | Value |
| --- | --- |
| `Reason()` | `v1.DisruptionReasonUnderutilized`, which keeps today's budgets. A new reason would need a CRD enum change (`nodepool.go:180`). |
| `Class()` | `GracefulDisruptionClass` |
| `ShouldDisrupt` | consolidation's (`consolidation.go:100`) |
| `ConsolidationType()` | a new value, `"pipeline"` |

Add a documented `opmetrics.Value` for `"pipeline"` to the `ConsolidationType` label (`metrics.go:37-56`), and a
pre-created timeout series (`:122-129`) if the method can time out. The pipeline's own `ConsolidationType()` labels
`EvaluationDurationSeconds` (`controller.go:199-202`) and, because `Command` embeds `Method` and `disrupt()` sets
`cmd.Method` (`controller.go:240`), the queue's `DecisionsPerformedTotal`, `NodepoolDecisionsPerformed` and
`DisruptionQueueFailuresTotal` (`queue.go:166-170`, `:453-477`). The wrapped upstream methods keep labelling
`ConsolidationTimeoutsTotal` and `FailedValidationsTotal` as `multi` and `single`
(`multinodeconsolidation.go:143`, `singlenodeconsolidation.go:67`, `validation.go:170`, `:188`).

### 8.2 Semantics the method must keep to reproduce two methods with one

Upstream runs Multi and Single as two `disrupt()` calls. Each call gets:

- a fresh `GetCandidatesWithTotals`;
- a fresh `SetNodePoolTotals`;
- a fresh `BuildDisruptionBudgetMapping`;
- its own `EvaluationDurationSeconds` sample.

Single runs only when Multi produced nothing (`controller.go:180-191`). A single `PipelineConsolidation` must reproduce
this exactly when configured with `moves: [upstream-multi, upstream-single]`:

1. **Rebuild between generators.** Before `upstream-single`, rebuild candidates, totals and budgets as `disrupt()`
   does.
   - Multi decremented the budget map in place (`multinodeconsolidation.go:74`).
   - Multi sorted the candidate slice in place (`consolidation.go:150`).
   - Multi may have waited 15 s in validation, during which state can change.
   - Calling the same helpers with the same arguments (`helpers.go:273`, `:417`) is enough.
2. **Two consolidated states.** Hold the two upstream method values, `*MultiNodeConsolidation` and
   `*SingleNodeConsolidation`, built with `MakeConsolidation` as `NewMethods` does.
   - Their separate `lastConsolidationState` values, Single's `PreviouslyUnseenNodePools` and their validators are part
     of the behavior.
   - Call `SetNodePoolTotals` on each before its generator runs.
   - Never share one `consolidation` value between them.
3. **Stop at the first command.** Run `upstream-single` only when `upstream-multi` returned no command. Return at most
   one command.
4. **Validate inside `ComputeCommands`.** Upstream validates inside `ComputeCommands`, before returning. The upstream
   planner must call the wrapped `Validator` with `commandValidationDelay`, in the same place, so the queue's
   `CreationTimestamp` ordering is unchanged.
5. **Leave execution to `disrupt()`.** `disrupt()` itself calls `StartCommand` on whatever `ComputeCommands` returns
   (`controller.go:233-246`).
   - The `upstream-queue` executor must therefore **return** merges as `Command`s rather than starting them;
     otherwise they would start twice.
   - It sets nothing that `disrupt()` overwrites (`CreationTimestamp`, `ID`, `Method`, `:238-240`).
   - Shift moves, which delete no node, need an execution path outside `Command` (Phase 3). Starting with a queue
     hand-off would be wrong, because `StartCommand` taints and deletes every candidate.

### 8.3 What each adapter wraps

| Stage | Adapter | Upstream function(s) wrapped | Notes |
| --- | --- | --- | --- |
| 1 Candidates | `upstream` | `GetCandidatesWithTotals` (`helpers.go:273`) with `consolidation.ShouldDisrupt` (`consolidation.go:100`), then `sortCandidates` (`:149`). For single: `SingleNodeConsolidation.SortCandidates` (`singlenodeconsolidation.go:139-170`). | Sources are the ordered candidates. Destinations are every active non-candidate node, the same set `SimulateScheduling` uses (`helpers.go:70-72`). Order is passed through unchanged; never re-sort inside the adapter. |
| 2 Moves | `upstream-single` | Coarse: `SingleNodeConsolidation.ComputeCommands` (`:53-124`). Fine, once parity holds: the loop body `CanPassThreshold`, then `computeConsolidation` (`consolidation.go:159`), then `ApproveCommand`. | The coarse form includes budget, threshold and validation; the fine form moves them to stage 3. |
| 2 Moves | `upstream-multi` | Coarse: `MultiNodeConsolidation.ComputeCommands` (`:50-110`). Fine: the logic of `firstNConsolidationOption` (`:115-189`) with `filterOutSameInstanceType` (`:207-243`). `firstNConsolidationOption` is unexported, so the fine form re-implements the binary search over the exports below (`ComputeConsolidation`, `FilterOutSameInstanceType`) and an exported `Evaluator`. | Search, scoring and the timeout are interleaved inside the binary search; split them only after the coarse form passes parity. |
| 3 Planner | `upstream` | `BuildDisruptionBudgetMapping` (`helpers.go:417`); each method's budget consumption (section 3.7); the evaluator production installs, `NewBalancedEvaluator(totals, recorder)` (`balanced.go:215`) through `SetNodePoolTotals` (`consolidation.go:71-73`), which scores only Balanced pools; the method's `Validator` (`validation.go:192`). | Reproduces production for every policy. |
| 3 Planner | `balanced` | The same, but scoring every NodePool as Balanced through `ScoreMove`/`EvaluateBalancedMove` (`balanced.go:109`, `:131`). | Equals production when every pool is Balanced; parity is tested on such fixtures. This is the hook for Balanced as one scorer among several. |
| 4 Executor | `upstream-queue` | Returns merges as `Command`s to `disrupt()`, which calls `Queue.StartCommand` (`queue.go:365`). Termination follows through the queue's `Reconcile` (`:143`) and the finalizers. | Rejects anything but merges. |

**Exports the adapters need from package `disruption`.** Keep these in one additive file; the working tree's
`pipeline_hooks.go` follows this pattern:

- `Candidate.ReschedulablePods()`, `InstanceType()`, `Zone()` and `CapacityType()`
- `consolidation.ComputeConsolidation` (wrapping `computeConsolidation`)
- `SortBySavingsRatio` (wrapping `sortCandidates`)
- `MarkConsolidated`
- `FilterOutSameInstanceType`

Apart from these and the documented `pipeline` value in `metrics.go` (section 8.1), nothing else in package
`disruption` has to change. `Evaluator`, `NewBalancedEvaluator`, `MakeConsolidation`, the validator constructors and
`ConsolidationTimeoutsTotal` are already exported; the new exported methods on the unexported `consolidation` type
reach the adapters through its embedding in `*MultiNodeConsolidation` and `*SingleNodeConsolidation`.

### 8.4 What the Snapshot copies from `state.Cluster`

**Capture** happens once per pass, right after `Synced`:

- Take one `cluster.DeepCopyNodes()` (`state/cluster.go:286`). Today a pass makes one copy in the controller
  (`controller.go:160`), then two more **per method**: one for candidates (`helpers.go:289`) and one for budgets
  (`helpers.go:347`). Every `SimulateScheduling` makes another (`helpers.go:68`).
- Sort by `ProviderID()`.
- Stamp `Version = cluster.ConsolidationState()` (`:649`; note that reading it can reset it) and `Now = clk.Now()`, and
  pass a frozen clock to every getter that takes a clock.

**Per node**

| Datum | Getter |
| --- | --- |
| identity | `ProviderID()` (`statenode.go:266`), `Name()` (`:250`), `HostName()` (`:358`) |
| labels, annotations | `maps.Clone(Labels())` (`:380`), `maps.Clone(Annotations())` (`:365`); never keep the returned maps |
| NodePool | `Labels()[v1.NodePoolLabelKey]` into `nodePoolMap` from `BuildNodePoolMap` (`helpers.go:311`) |
| instance type | `nodePoolToInstanceTypesMap[np][Labels()[corev1.LabelInstanceTypeStable]]` (`types.go:194`). Share the pointer: `InstanceType` contains a `sync.Once` (`cloudprovider/types.go:194`) and must not be copied by value. |
| zone, capacity type | `Labels()[corev1.LabelTopologyZone]`, `Labels()[v1.CapacityTypeLabelKey]` (`types.go:219-220`) |
| price | `cloudprovider.PriceNodeClaim(ctx, node.NodeClaim, it)` (`cloudprovider/types.go:919`). The default provider reads the **NodeClaim's** labels (`:888-893`). |
| taints | `Taints()` (`:406`) |
| resources | `Capacity()` (`:458`), `Allocatable()` (`:476`), `Available()` (`:494`), `PodRequests()` (`:514`), `DaemonSetRequests()` (`:498`) |
| lifecycle | `Initialized()` (`:449`), `Registered()` (`:440`), `Managed()` (`:557`), `MarkedForDeletion()` (`:536`), `RebootInProgress()` (`:398`), `Nominated(frozenClk)` (`:553`) |
| disruptable | the error from `ValidateNodeDisruptable(frozenClk)` (`:286`), with `IsNodeDoNotDisruptError` / `IsNodeUninitializedError` (`:89`, `:114`) |
| consolidatable | `NodeClaim.StatusConditions().Get(v1.ConditionTypeConsolidatable).IsTrue()` (`consolidation.go:135`); `NodePool.Spec.Disruption.ConsolidateAfter/ConsolidationPolicy`; `NodePool.Spec.Replicas` (static) |
| disruption cost | the candidate's `RescheduleDisruptionCost`, and `StateNode.DisruptionCost()` (`:528`) for non-candidates, **exactly as `computeNodePoolTotals` reads them** (section 4.2) |
| lifetime, TGP | `NodeClaim.Spec.ExpireAfter`, `NodeClaim.CreationTimestamp` (`LifetimeRemaining`), `NodeClaim.Spec.TerminationGracePeriod` |
| in flight | `queue.HasAny(providerID)` (`queue.go:480`), `cluster.HasBufferPods(providerID)` (`cluster.go:338`) |
| simulation handle | the frozen `*state.StateNode`. **Each feasibility fork must `DeepCopy()` again** (`zz_generated.deepcopy.go:145`), because `ExistingNode` mutates `VolumeUsage` and `HostPortUsage`. |

**Per pod**

- `StateNode.Pods(ctx, kubeClient)` (`:274`). It lists by `spec.nodeName` without `UnsafeDisableDeepCopy`
  (`pkg/utils/node/node.go:109-124`), so a cached client already returns copies, not cache pointers. Keep them
  without copying again, and treat them as read-only once the Snapshot shares them across stages.
- Derived values:
  - `resources.RequestsForPods`
  - `podutils.IsReschedulable` (`pkg/utils/pod/scheduling.go:45`), `IsOwnedByDaemonSet` (`:169`),
    `IsDisruptable(p, frozenClk, recorder)` (`:116`)
  - `disruptionutils.EvictionCost` (`disruption.go:97`)
  - PDB membership from `pdb.NewLimits` (`pkg/utils/pdb/pdb.go:45`), with `CanEvictPods` (`:66`) and
    `IsCurrentlyReschedulable` (`:138`)

**Cluster level**

- Budgets per reason: `NodePoolStatsFromNodes(snapshotNodes, reason)` (`helpers.go:370`) + `NodePoolBudgetMap`
  (`:350`). This variant is pure; it emits no metrics or events.
- Totals from `computeNodePoolTotals` (`balanced.go:48`), or from the totals the controller handed the method.
- `NodePoolResourcesFor` (`cluster.go:664`) for NodePool limits.

**What the Snapshot cannot replace yet.** `SimulateScheduling` also reads live state behind its arguments:

- `GetPendingPods` (`helpers.go:83`)
- PDBs (`:101`)
- deleting-node pods (`:113`)
- NodePools and instance types (`provisioner.go:289-331`)
- topology pod, node and namespace lists (`scheduling/topology.go:367-372`, `:420`, `:554`)
- `ForPodsWithAntiAffinity` (`topology.go:315`)
- DaemonSets (`provisioner.go:354`)
- `cluster.Nodes()` (`scheduler.go:238`)

The scheduler is wired to `p.kubeClient` and `p.cluster`. `Feasibility.Full` should therefore keep calling
`SimulateScheduling` against the live (or frozen fake) client and cluster. `Feasibility.Fast` runs on the Snapshot
alone.

**Side effects to keep out of capture**

- the Blocked events `NewCandidate` publishes (`types.go:159`, `:174`, `:190`, `:209`)
- invalid do-not-disrupt events (`pkg/utils/pod/scheduling.go:255-257`)
- `MarkPodSchedulingDecisions` through `GetPendingPods`

Route these through the Recorder, or accept that the upstream candidate adapter emits them exactly as upstream does.

### 8.5 Converting a `Command` to a `Move` and back

**Command to Move (merge)**

| Move field | Source |
| --- | --- |
| `Kind` | `"merge"` |
| `Sources` = `Remove` | `cmd.SourceNodeNames()` (`types.go:345`) |
| `Launch[i]` | from `cmd.Replacements[i]`: `NodePoolName`, the replacement `InstanceTypeOptions` names **in order**, `Requirements`, and the pod keys in `Replacements[i].Pods` |
| `Bindings` for pods on existing nodes | for each `Results.ExistingNodes[j]`, every pod in `.Pods` whose key matches a candidate's `ReschedulablePods()` maps to `Target{Node: ExistingNodes[j].Name()}` |
| `Bindings` for pods on new nodes | every candidate pod in `NewNodeClaims[i].Pods` maps to `Target{Launch: i}` |
| `Upstream` | the original `Command`, kept as an opaque payload |

Rules for building the bindings:

- Match pods by `client.ObjectKey`, never by pointer.
- Exclude pending pods and deleting-node pods. They also appear in `Results`.
- Pods that a fully blocking PDB kept out of the simulation have no binding. If a snapshot-level `Apply` needs them,
  place them there and mark them as such.
- Never derive a LaunchSpec zone or capacity type with `Requirement.Any()` (section 6). Store the full requirement
  values, sorted, or the minimum.

**Move to Command**

- **Moves that came from upstream:** return the carried `Command` unchanged. This is required for exact parity:
  - The queue needs `Replacements[i].NodeClaim` (a `*pscheduling.NodeClaim`) for `CreateNodeClaims` (`queue.go:347`).
  - It needs `Results` for `Results.Record` nominations (`queue.go:436`).
  - It needs the `*Candidate`s, with `reschedulablePods`, for metrics (`queue.go:286`).
  - Neither the replacement nor the nominations can be rebuilt from a `LaunchSpec`.
- **Pipeline-native merges:**
  1. Map `Sources` to this pass's `*Candidate`s by name (as `mapCandidates` does, `helpers.go:441-446`).
  2. Run `ComputeConsolidation(ctx, candidates...)`, which re-simulates and applies the price and spot rules.
  3. Accept the result only if it is not a no-op and its replacement count matches `len(Launch)`.

  That is the `Full` feasibility check, and its output is the Command the queue runs.
- **Shift moves** do not convert, because they delete no node (section 8.2).

### 8.6 What the parity harness must hold fixed

**Clocks and timing**

- Use one fake clock (`env.Clock`, or `clocktesting.FakeClock` in the simulator) for the cluster, provisioner,
  queue, methods and validators.
- Never step it inside a compared call. Step it only to pass `consolidateAfter`, then call `cluster.MarkUnconsolidated()`.
- Inject validators through `WithValidator`: either `NopValidator` (`validation_test.go:103-107`), or a wrapper that
  calls the real validator with a delay of 0, as `validation_test.go:245` does. Both live in package `disruption_test`,
  so a parity test in package `pipeline` needs its own copies. The methods always pass the 15 s
  `commandValidationDelay` themselves, so do not try to step the clock through that wait.
- Keep every case far below the 1-minute wall-clock multi-node deadline, and assert that `ConsolidationTimeoutsTotal`
  did not change.

**Cloud provider and global state**

- Use the same instance-type objects for both paths: `cloudProvider.InstanceTypes` (or `InstanceTypesForNodePool`),
  built once per case.
- Avoid reserved offerings, or reset the provider between paths, because `Create` mutates `ReservationCapacity`
  (`fake/cloudprovider.go:188-189`).
- Keep the global price provider at its default.
- Pass the same feature gates (`options.ToContext`) to both paths. `SpotToSpotConsolidation`, `CapacityBuffer`,
  `PodDeletionCostManagement` and `ReservedCapacity` all change decisions.

**Cluster state**

- Freeze state between the two paths: no informer writes, the same `Synced` state, the same queue contents.
- Use the same `ClusterCost` values, because `TotalCost` reads it live.
- Give each path its own `MakeConsolidation` value, starting with `lastConsolidationState` zero and
  `PreviouslyUnseenNodePools` empty.

**Ordering and randomness**

- Fix the order: build the candidate slice once and give both paths copies, or pre-sort by name.
- Use one NodePool, or record the legacy pool order.
- Seed all randomness from the case seed: object names, UIDs, prices and pod sizes.
- The pipeline's RNG is not used by the upstream adapters, but pass it anyway so the conformance suite exercises it.

**What to compare**

- Compare `{Decision(), sorted candidate names, replacement NodePool, sorted replacement instance-type names}`.
- Compare Unconsolidatable, ConsolidationCandidate, ConsolidationApproved and ConsolidationRejected events as
  multisets, excluding message text that embeds map order.
- Compare `ConsolidationMovesTotal` and `ConsolidationScoreHistogram` **deltas**.

### 8.7 Risks

1. **Hidden state across passes.** `lastConsolidationState` and `PreviouslyUnseenNodePools` make results depend on
   history. A pipeline that rebuilds its upstream methods every pass loses the short-circuit and changes timing.
   Upstream decisions on the next pass stay the same, except after a single-node timeout, when the lost
   `PreviouslyUnseenNodePools` would have reordered the next scan (`singlenodeconsolidation.go:70`, `:150-153`); the
   evaluation load and the metrics change either way.
2. **Validation timing.** Moving validation out of `ComputeCommands` shifts `CreationTimestamp` relative to the 15 s
   wait and changes when the queue times out.
3. **Metric label drift.** A new `consolidation_type` value splits dashboards. Section 8.1 says which metrics keep
   `multi` and `single`.
4. **Bug-compatible quirks.** Exact parity has to reproduce all of these:
   - the multi-node window of up to 101 nodes (`maxParallel` is 100, `multinodeconsolidation.go:79`, and the window is
     `candidates[0:mid+1]`, `:136`);
   - no rejection events after a multi-node validation failure, because `Validate` returns an empty `Command` on error
     and that empty value is what `EmitRejectedEvents` runs on (`multinodeconsolidation.go:101-105`, `validation.go:193-194`);
   - `CanPassThreshold` passing missing totals;
   - `moves_total` counting non-Balanced pools;
   - `filterOutSameInstanceType` removing every option when a source with an unknown price (`Price <= 0`) shares an
     instance type with the replacement options, because the missing price reads as a 0 ceiling
     (`multinodeconsolidation.go:214-216`, `:231-238`);
   - `podDisruptionCosts` resetting on NodeClaim updates.

   Document each one in `docs/research/parity.md` before "fixing" it in pipeline-native strategies.
5. **Shared mutable objects.**
   - Candidate `NodePool` pointers are cache-backed.
   - `StateNode.Labels()` returns the live map.
   - `results.NewNodeClaims[0]` is mutated by the price filters.
   - `ExistingNode` mutates usage objects.

   Any adapter that keeps a reference across stages must copy it.
6. **Import cycles.** `pipeline` imports `disruption`, so nothing in `disruption` may import `pipeline`, including
   tests in package `disruption`. Parity tests belong in package `pipeline` or `disruption_test`.
7. **Scale.** A full single-node scan at 1,000 nodes costs about 58 s of wall time (`experiments/RESEARCH_LOG.md`).
   `SimulateScheduling` makes a deep copy of every node and a PDB list per call. Feasibility budgets must count full
   simulations, not moves.
8. **Gate leakage.** The plan requires everything to default off. The gate check must sit only in the wiring
   (section 8.1). Tests that build methods by hand must not pick up the pipeline unless the gate is set in their
   context.
9. **envtest-only taint.** Fixtures must strip `node.kubernetes.io/not-ready` (section 7.8), or existing nodes reject
   rescheduled pods and decisions differ from production.
