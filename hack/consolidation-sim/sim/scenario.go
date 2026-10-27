/*
Copyright The Kubernetes Authors.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package sim

import (
	"fmt"
	"math"
	"math/rand"
	"sort"
	"time"
)

// ScenarioConfig picks a workload and its scale. Nodes sizes the workload so it needs roughly that many nodes at peak.
type ScenarioConfig struct {
	Name  string         `json:"name"`
	Nodes int            `json:"nodes"`
	Param map[string]any `json:"params,omitempty"`
}

// Workloads.
const (
	W1Fragmented  = "W1"
	W2Diurnal     = "W2"
	W3BatchChurn  = "W3"
	W4MixedShapes = "W4"
	W5Constrained = "W5"
	W6Trace       = "W6"
)

// Shape is a pod's requests: CPU in millicores, memory in MiB.
type Shape struct {
	CPU    int64 `json:"cpu"`
	Memory int64 `json:"memory"`
}

// Deployment is a ReplicaSet-managed workload whose replica count follows Schedule.
type Deployment struct {
	Name  string `json:"name"`
	Shape Shape  `json:"shape"`
	// Schedule is a step function: replicas from each point's time until the next point.
	Schedule []ReplicaPoint `json:"schedule"`
	// PDBMaxUnavailable adds a PodDisruptionBudget when >= 0; -1 means none.
	PDBMaxUnavailable int `json:"pdbMaxUnavailable"`
	// ZoneSpread adds a hard zone topology spread constraint (maxSkew 1). Not used by the built-in scenarios: when zones
	// tie for the minimum count, Karpenter's scheduler pins a new NodeClaim to whichever it meets first while ranging
	// over a map (provisioning/scheduling/topologygroup.go), so runs with it are not reproducible.
	ZoneSpread bool `json:"zoneSpread,omitempty"`
	// HostSpread adds a hard hostname topology spread constraint (maxSkew 2), deterministic in Karpenter's scheduler
	// because a new NodeClaim has exactly one hostname domain.
	HostSpread bool `json:"hostSpread,omitempty"`
	// HostAntiAffinity adds required hostname anti-affinity between replicas.
	HostAntiAffinity bool `json:"hostAntiAffinity,omitempty"`
	// DoNotDisrupt annotates replicas with karpenter.sh/do-not-disrupt.
	DoNotDisrupt bool `json:"doNotDisrupt,omitempty"`
	// EvictionCost, when non-zero, sets karpenter.sh/disruption-cost on replicas.
	EvictionCost int `json:"evictionCost,omitempty"`
}

// ReplicaPoint is one step of a replica schedule.
type ReplicaPoint struct {
	At       Duration `json:"at"`
	Replicas int      `json:"replicas"`
}

// JobClass is a batch job stream: Poisson arrivals, lognormal durations.
type JobClass struct {
	Name  string `json:"name"`
	Shape Shape  `json:"shape"`
	// RatePerHour is the arrival rate; arrivals stop at Until.
	RatePerHour float64  `json:"ratePerHour"`
	Until       Duration `json:"until"`
	// MedianDuration and Sigma parameterize a lognormal duration, heavy-tailed for sigma >= 1.
	MedianDuration Duration `json:"medianDuration"`
	Sigma          float64  `json:"sigma"`
}

// InitialNode is a node that exists at t = 0, with pods placed on it, for fragmented starts.
type InitialNode struct {
	InstanceType string         `json:"instanceType"`
	Zone         string         `json:"zone"`
	Pods         map[string]int `json:"pods"` // deployment -> replicas placed here
}

// Task is one pod with a fixed arrival time and run time, for trace replay (W6). A task evicted before it
// finishes restarts with its full duration, like a Job's pod.
type Task struct {
	Start    Duration `json:"start"`
	Duration Duration `json:"duration"`
	Shape    Shape    `json:"shape"`
}

// Workload is a generated scenario.
type Workload struct {
	Name        string        `json:"name"`
	Deployments []Deployment  `json:"deployments"`
	Jobs        []JobClass    `json:"jobs,omitempty"`
	Initial     []InitialNode `json:"initial,omitempty"`
	Tasks       []Task        `json:"tasks,omitempty"`
}

func (d Deployment) replicasAt(t time.Duration) int {
	r := 0
	for _, p := range d.Schedule {
		if p.At.Duration <= t {
			r = p.Replicas
		}
	}
	return r
}

func (d Deployment) peak() int {
	m := 0
	for _, p := range d.Schedule {
		m = max(m, p.Replicas)
	}
	return m
}

// Generate builds a workload from its config. The catalog's mean allocatable sets how many pods make Nodes nodes.
//
//nolint:gocyclo
func Generate(cfg ScenarioConfig, rng *rand.Rand, cat catalogView) (Workload, error) {
	n := max(cfg.Nodes, 2)
	switch cfg.Name {
	case W1Fragmented:
		return fragmented(n, rng, cat, standardShapes, false), nil
	case W4MixedShapes:
		return fragmented(n, rng, cat, mixedShapes, true), nil
	case W2Diurnal:
		return diurnal(n, rng, cat, false), nil
	case W5Constrained:
		return diurnal(n, rng, cat, true), nil
	case W3BatchChurn:
		return batchChurn(n, rng, cat), nil
	case W6Trace:
		return traceReplay(cfg, n, rng, cat)
	}
	return Workload{}, fmt.Errorf("unknown scenario %q", cfg.Name)
}

// catalogView is what scenario generation needs from the catalog.
type catalogView interface {
	// Types returns instance types eligible for initial nodes, with allocatable CPU (m) and memory (MiB).
	Types() []typeInfo
	Zones() []string
}

type typeInfo struct {
	Name   string
	CPU    int64
	Memory int64
}

var standardShapes = []Shape{{250, 512}, {500, 1024}, {1000, 2048}, {1000, 4096}, {2000, 4096}, {2000, 8192}, {500, 2048}, {4000, 8192}}

// mixedShapes are CPU-heavy (1 CPU : 1 GiB) and memory-heavy (1 CPU : 8 GiB), for multi-dimensional stranding.
var mixedShapes = []Shape{{1000, 1024}, {2000, 2048}, {4000, 4096}, {500, 4096}, {1000, 8192}, {2000, 16384}}

// fragmented places replicas randomly over randomly sized initial nodes, each filled to a random 15-75%: a cluster
// left fragmented by past scale-downs, with no further churn.
func fragmented(n int, rng *rand.Rand, cat catalogView, shapes []Shape, mixed bool) Workload {
	w := Workload{Name: pick(mixed, W4MixedShapes, W1Fragmented)}
	types := cat.Types()
	sizes := filterTypes(types, func(t typeInfo) bool { return t.CPU >= 3500 && t.CPU <= 16000 })
	deps := max(4, n/3)
	for i := range deps {
		w.Deployments = append(w.Deployments, Deployment{Name: fmt.Sprintf("app-%03d", i), Shape: shapes[rng.Intn(len(shapes))], PDBMaxUnavailable: -1})
	}
	counts := make([]int, deps)
	for range n {
		t := sizes[rng.Intn(len(sizes))]
		node := InitialNode{InstanceType: t.Name, Zone: cat.Zones()[rng.Intn(len(cat.Zones()))], Pods: map[string]int{}}
		target := 0.15 + 0.6*rng.Float64()
		var cpu, mem int64
		for attempt := 0; attempt < 50; attempt++ {
			di := rng.Intn(deps)
			s := w.Deployments[di].Shape
			if float64(cpu+s.CPU) > target*float64(t.CPU) || mem+s.Memory > t.Memory*95/100 {
				continue
			}
			cpu, mem = cpu+s.CPU, mem+s.Memory
			node.Pods[w.Deployments[di].Name]++
			counts[di]++
		}
		w.Initial = append(w.Initial, node)
	}
	for i := range w.Deployments {
		w.Deployments[i].Schedule = []ReplicaPoint{{At: D(0), Replicas: counts[i]}}
	}
	return w
}

// diurnal ramps deployments to a peak that needs about n nodes, then scales each down by 50-70% over the second hour
// and holds. constrained adds PDBs, hard hostname spread, hostname anti-affinity and do-not-disrupt pods (W5).
func diurnal(n int, rng *rand.Rand, cat catalogView, constrained bool) Workload {
	w := Workload{Name: pick(constrained, W5Constrained, W2Diurnal)}
	capCPU := float64(n) * meanCPU(cat) * 0.7
	deps := max(4, n/4)
	perDep := capCPU / float64(deps)
	for i := range deps {
		s := standardShapes[rng.Intn(len(standardShapes))]
		peak := max(2, int(perDep/float64(s.CPU)*(0.5+rng.Float64())))
		drop := 0.5 + 0.2*rng.Float64()
		low := max(1, int(math.Round(float64(peak)*(1-drop))))
		down := 60*time.Minute + time.Duration(rng.Intn(60))*time.Minute
		d := Deployment{Name: fmt.Sprintf("app-%03d", i), Shape: s, PDBMaxUnavailable: -1,
			Schedule: []ReplicaPoint{{At: D(0), Replicas: peak}, {At: D(down), Replicas: low}}}
		if constrained {
			r := rng.Float64()
			if r < 0.5 {
				d.PDBMaxUnavailable = 1
			}
			d.HostSpread = rng.Float64() < 0.3
			d.HostAntiAffinity = !d.HostSpread && rng.Float64() < 0.1
			d.DoNotDisrupt = rng.Float64() < 0.05
		}
		w.Deployments = append(w.Deployments, d)
	}
	return w
}

// batchChurn runs steady services at about 40% of n nodes plus job streams that add about 60% more at their mean,
// with heavy-tailed durations, so nodes empty and refill unpredictably.
func batchChurn(n int, rng *rand.Rand, cat catalogView) Workload {
	w := Workload{Name: W3BatchChurn}
	capCPU := float64(n) * meanCPU(cat) * 0.7
	deps := max(3, n/6)
	for i := range deps {
		s := standardShapes[rng.Intn(len(standardShapes))]
		reps := max(1, int(0.4*capCPU/float64(deps)/float64(s.CPU)))
		w.Deployments = append(w.Deployments, Deployment{Name: fmt.Sprintf("svc-%03d", i), Shape: s, PDBMaxUnavailable: -1,
			Schedule: []ReplicaPoint{{At: D(0), Replicas: reps}}})
	}
	classes := 4
	for i := range classes {
		s := standardShapes[rng.Intn(len(standardShapes))]
		median := time.Duration(5+rng.Intn(20)) * time.Minute
		// Little's law: mean concurrent jobs = rate * mean duration; mean of a lognormal is median * exp(sigma^2/2).
		sigma := 1.0 + 0.5*rng.Float64()
		meanDur := median.Hours() * math.Exp(sigma*sigma/2)
		concurrent := 0.6 * capCPU / float64(classes) / float64(s.CPU)
		w.Jobs = append(w.Jobs, JobClass{Name: fmt.Sprintf("job-%d", i), Shape: s, RatePerHour: concurrent / meanDur,
			Until: D(150 * time.Minute), MedianDuration: D(median), Sigma: sigma})
	}
	return w
}

func meanCPU(cat catalogView) float64 {
	ts := filterTypes(cat.Types(), func(t typeInfo) bool { return t.CPU >= 3500 && t.CPU <= 16000 })
	var s float64
	for _, t := range ts {
		s += float64(t.CPU)
	}
	return s / float64(len(ts))
}

func filterTypes(ts []typeInfo, f func(typeInfo) bool) []typeInfo {
	var out []typeInfo
	for _, t := range ts {
		if f(t) {
			out = append(out, t)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

func pick[T any](c bool, a, b T) T {
	if c {
		return a
	}
	return b
}

// traceReplay is W6. No public trace is bundled with the repository and the session's network policy does not reach
// the trace hosts, so W6 needs a trace file (params.trace: CSV of start_seconds,end_seconds,cpu_millicores,memory_mib)
// prepared by hack/consolidation-sim/traces; without one it fails, and the runner records the cell as skipped.
func traceReplay(cfg ScenarioConfig, n int, rng *rand.Rand, cat catalogView) (Workload, error) {
	path, _ := cfg.Param["trace"].(string)
	if path == "" {
		return Workload{}, fmt.Errorf("scenario W6 needs params.trace (a CSV prepared by hack/consolidation-sim/traces)")
	}
	return loadTrace(path, n, rng, cat)
}
