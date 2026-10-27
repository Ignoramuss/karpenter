# Trace replay (W6)

W6 replays a window of a public cluster trace through the simulator, so the
consolidation results can be checked against a workload nobody tuned. This
directory holds the derived trace and the tool that makes it.

| File | What it is |
| --- | --- |
| `google-2011-6h-9h.csv` | The derived trace: 49,675 task runs from hours 6 to 9 of the Google 2011 trace (918 KB) |
| `google-2011-6h-9h.provenance.json` | The inputs' SHA-256 sums, the flags used, and counts for every dropped run |
| `main.go`, `derive.go` | The tool (`package main`, Go standard library only) |
| `derive_test.go` | Tests of the mapping on hand-written events |

## Source and license

The source is the **Google cluster-usage trace 2011, version 2.1**
(`clusterdata-2011-2`): 29 days of one Borg cell of about 12.5k machines,
May 2011, by John Wilkes and Charles Reiss. It is published in the bucket
<https://storage.googleapis.com/clusterdata-2011-2/> and described in
<https://github.com/google/cluster-data/blob/master/ClusterData2011_2.md>.

The bucket itself holds no license file; its `README` reads, in part, "This
directory contains a Google cluster trace generated at 2014-11-18 09:58Z." The
license is stated in the `google/cluster-data` repository. Quoted from
`README.md` and `ClusterData2011_2.md` at commit
`48b12446464b0422abcc18e0ec5b0b13e2f3a90c` (fetched 2026-10-27; the repository
has no separate LICENSE file):

> The data and trace documentation are made available under the
> [CC-BY](https://creativecommons.org/licenses/by/4.0/) license. By downloading
> it or using them, you agree to the terms of this license.

The link names **CC BY 4.0**. The derived CSV and its provenance file are an
adaptation of the trace and stay under CC BY 4.0, not under the Apache 2.0
license of the code here. Attribution, with the changes CC BY asks to be
indicated:

> Derived from the Google cluster-usage trace `clusterdata-2011-2` (John Wilkes
> and Charles Reiss, Google Inc.,
> <https://github.com/google/cluster-data/blob/master/ClusterData2011_2.md>),
> licensed under CC BY 4.0 (<https://creativecommons.org/licenses/by/4.0/>).
> Changes: one 3-hour window of `task_events` reduced to task runs; requests
> scaled to absolute units and clamped to minimums; 20% of tasks sampled. See
> `hack/consolidation-sim/traces/README.md`.

To cite the trace, the repository's `bibliography.bib` asks for both of these
entries:

- C. Reiss, J. Wilkes, J. L. Hellerstein. *Google cluster-usage traces: format +
  schema.* Technical report, Google Inc., Nov 2011, revised 2014-11-17 for
  version 2.1 (`clusterdata:Reiss2011`).
- J. Wilkes. *More Google cluster data.* Google research blog, Nov 2011
  (`clusterdata:Wilkes2011`).

The format document is a PDF on Google Drive, which this session's network
policy refuses (HTTP 403). The facts below that come from it (event type
numbers, the microsecond clock, normalization to the largest machine) are
cross-checked against the bucket's `schema.csv` and against the data, as noted.

## Mapping

The tool reads `task_events` (schema from the bucket's `schema.csv`: field 1
time, 3 job ID, 4 task index, 6 event type, 10 CPU request, 11 memory request).

1. **Clock.** Timestamps are microseconds. The trace starts at timestamp 600 s,
   19:00 EDT on Sunday May 1, 2011 (`ClusterData2011_2.md`). Each shard is
   sorted by time and the shards follow each other (about 5,010 s each); the
   tool fails if an event goes back in time.
2. **Window.** `[start, start + 3h)` with start 6 h after the trace start:
   trace timestamps 22,200 s to 33,000 s, 01:00 to 04:00 EDT on Monday May 2,
   2011. The offset keeps the window clear of how the recording began: every
   task already running when tracing started is stamped 0 (132,982 SCHEDULE
   events), and the first hour schedules 45,777 tasks against 21,396 and
   19,726 in the next two (the fourth hour is as busy again, 45,963, so the
   first hour is not unique). Six hours is a round offset, fixed before any
   simulation ran. The long-running tasks of that initial state remain:
   108,200 of the 140,502 tasks running at the window start were scheduled at
   time 0.
3. **Runs.** A task is (job ID, task index). A run starts at a SCHEDULE event
   (type 1) and ends at the task's next EVICT, FAIL, FINISH, KILL or LOST
   (types 2 to 6). A task evicted and scheduled again yields one row per run.
   SUBMIT, UPDATE_PENDING and UPDATE_RUNNING (0, 7, 8) are ignored, so a
   request changed while running keeps its SCHEDULE-time value (138
   UPDATE_RUNNING events in the first 18 hours). A terminal event with no open
   run (a task killed while pending) is counted and skipped; a SCHEDULE for a
   task already running closes the earlier run there (none occur in this
   window).
4. **Clipping.** Runs overlapping the window are kept. A run that started
   before the window starts at 0; one still running at the window end ends at
   10,800 s. That needs every shard from the trace start, so the tool reads
   shards 0 to 6. Start seconds are floored and end seconds ceiled, so a
   sub-second run lasts one second; a run of zero length is dropped.
5. **Requests.** The CPU and memory requests are normalized to the largest
   machine's capacity, 1.0. The machine table agrees: in
   `machine_events/part-00000-of-00001.csv.gz` the CPU capacities are 0.25, 0.5
   and 1.0, and the largest memory capacity is 1.0. The trace never states the
   absolute size, so the scale factors are assumptions:
   - **CPU: 32 cores** per 1.0, so `cpu_millicores = round(cpu * 32000)`.
   - **Memory: 128 GiB** per 1.0, so `memory_mib = round(memory * 131072)`.

   With these factors the largest machine (1.0 CPU, 1.0 memory; 788 of the
   12,477 machines present at the trace start) is 32 vCPU and 128 GiB, and the
   most common one (0.5, 0.4995; 6,672 machines) is 16 vCPU and 64 GiB. Those
   are the m7i.8xlarge and m7i.4xlarge shapes of the simulator's catalog, 4
   GiB per vCPU. At the window's peak the requests average 3.04 GiB per
   requested core, between c7i (2) and m7i (4).
6. **Minimums.** Requests are clamped up to 100m CPU and 128 MiB: the trace has
   requests that scale to a few millicores. Rows with an empty request on
   their SCHEDULE event would be dropped; this window has none.
7. **Sampling.** 20% of tasks are kept, chosen by task rather than by run, so
   every run of a sampled task stays together. A task is kept when the first 8
   bytes of `SHA-256("<seed>/<job ID>/<task index>")`, read big-endian, are
   below 0.2 × 2⁶⁴, with seed 1. A hash makes the choice independent of event
   order and reproducible from this description alone.
8. **Output.** Rows are sorted by start, end, CPU and memory, under the header
   `start_seconds,end_seconds,cpu_millicores,memory_mib`.

### What the window holds

| | Full window | Derived file (20%) |
| --- | --- | --- |
| Task runs | 245,895 | 49,675 |
| Running at the window start (start 0) | 140,502 | 28,181 |
| Still running at the window end (end 10,800) | 144,054 | 28,793 |
| Peak concurrent CPU request | 248,720 cores | 49,972 cores |
| CPU clamped to 100m / memory clamped to 128 MiB | 5,744 / 1,504 | 1,151 / 328 |

In the derived file, runs that start inside the window last 32 s at the 10th
percentile, 483 s at the median and 4,725 s at the 90th. CPU requests are 400m
at the 25th percentile, 1000m at the median and 3,199m at the 90th, up to
14,000m. Memory requests are 2,036 MiB, 2,872 MiB and 11,232 MiB at the same
percentiles, up to 66,689 MiB. The sample tracks the full window: these
quantiles match the full window's to within 6%, and its peak is 20.1% of the
full peak. Two counts of the full window also match a separate script run over
the same shards: 140,502 tasks running at 22,200 s and 144,054 at 33,000 s.

## Re-running the tool

From the repository root:

```sh
go run ./hack/consolidation-sim/traces --cache /tmp/clusterdata-2011-2
```

It downloads the bucket's `SHA256SUM` and shards
`task_events/part-00000-of-00500.csv.gz` to `part-00006` (about 13 MB) into
`--cache`, checks every shard's SHA-256 against `SHA256SUM`, and rewrites
`google-2011-6h-9h.csv` and `google-2011-6h-9h.provenance.json` here. Shards
already in the cache are reused after the same check. The provenance file
holds no timestamps or local paths, so a re-run with the same flags is byte
for byte identical: the CSV's SHA-256 is
`c9c2e51820a081f85a42cf34c17da36fd581f37fd33329b1b0aa26e86f969e3f`.

To fetch the inputs yourself, mirror the bucket layout and pass `--offline`:

```sh
mkdir -p /tmp/clusterdata-2011-2/task_events
gsutil cp gs://clusterdata-2011-2/SHA256SUM /tmp/clusterdata-2011-2/
gsutil cp 'gs://clusterdata-2011-2/task_events/part-0000[0-6]-of-00500.csv.gz' /tmp/clusterdata-2011-2/task_events/
go run ./hack/consolidation-sim/traces --cache /tmp/clusterdata-2011-2 --offline
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--cache` | (required) | Directory for `SHA256SUM` and `task_events/` shards |
| `--offset` | `6h` | Window start after the trace start |
| `--window` | `3h` | Window length |
| `--sample` | `0.2` | Fraction of tasks kept |
| `--seed` | `1` | Seed of the task sample |
| `--cpu-cores` | `32` | Cores per normalized CPU unit |
| `--memory-gib` | `128` | GiB per normalized memory unit |
| `--out` | `hack/consolidation-sim/traces/google-2011-<window>.csv` | Output; the provenance file goes next to it |
| `--offline` | `false` | Fail instead of downloading a missing input |
| `--bucket` | `https://storage.googleapis.com/clusterdata-2011-2` | Where missing inputs come from |

A different `--offset` or `--window` names the file after its window, for
example `google-2011-24h-27h.csv`, and reads more shards. `--sample 1` keeps
every task (4.5 MB for this window).

## How the simulator uses it

A W6 cell names the file in its scenario parameters, relative to the working
directory:

```yaml
scenario:
  name: W6
  nodes: 200
  params:
    trace: hack/consolidation-sim/traces/google-2011-6h-9h.csv
```

`loadTrace` (`sim/trace.go`) reads the CSV strictly and fails on a missing
file, a wrong header or a bad row. It drops tasks that fit no instance type in
the catalog. It then scales the task set so its peak concurrent CPU request
matches `nodes` × the catalog's mean allocatable CPU (the 3.5 to 16 vCPU band
W2, W3 and W5 use, 9,233m for the default catalog) × 0.7:

- **Smaller targets** take a uniform random subset: a prefix of a seeded
  permutation, its length found by binary search on the peak. With seed 1,
  20 nodes take 115 tasks and 200 nodes take 1,331.
- **Larger targets** take whole copies (the first unchanged, the rest with
  start times jittered by up to ±1 minute) plus a prefix of one more jittered,
  permuted copy. 10,000 nodes take 63,920 tasks.

Either way the peak lands within one task's request of the target, and the
run's seed fixes the result. Each task becomes a Job-owned pod with the
task's requests, created at its start and deleted when its duration has run
after it became ready. A task evicted by consolidation restarts with its full
duration.

## Limitations

- Requests, not usage: Borg overcommits, and the window's requests total about
  1.2× the cell's CPU capacity. Scaling to `nodes` absorbs the absolute level;
  the shape of arrivals, lifetimes and request sizes is what W6 tests.
- Priorities, scheduling classes and `task_constraints` are ignored. Borg's
  own preemptions show up as runs ending in EVICT, with the task's next run as
  a separate row.
- Tasks running when the window opens all arrive at t = 0, as a burst.
- The window is a night (01:00 to 04:00 EDT). Use `--offset` for other hours.
- The CPU and memory factors are assumptions, documented above. The memory
  factor sets the memory-to-CPU mix and so which instance families fit best.
