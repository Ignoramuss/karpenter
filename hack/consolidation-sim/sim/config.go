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
	"encoding/json"
	"fmt"
	"time"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/harness/catalog"
	_ "sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/strategies/all"
)

// Duration is a time.Duration that reads and writes as a string ("90s", "4h").
type Duration struct{ time.Duration }

func (d Duration) MarshalJSON() ([]byte, error) { return json.Marshal(d.String()) }

func (d *Duration) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err != nil {
		return err
	}
	v, err := time.ParseDuration(s)
	if err != nil {
		return err
	}
	d.Duration = v
	return nil
}

// D is shorthand for Duration{d}.
func D(d time.Duration) Duration { return Duration{d} }

// Arms. Baselines B0-B5 follow the plan; "pipeline" runs Arm.Pipeline.
const (
	ArmNone        = "B0-none"
	ArmWhenEmpty   = "B1-when-empty"
	ArmUnderutil   = "B2-when-empty-or-underutilized"
	ArmBalanced    = "B3-balanced"
	ArmPipeline    = "pipeline"
	ArmDescheduler = "B5-descheduler"
)

// Placement models: where kube-scheduler puts a pending pod.
const (
	LeastAllocated = "least-allocated"
	MostAllocated  = "most-allocated"
	PlanFollowing  = "plan-following"
)

// Scale-down models: which replicas a ReplicaSet deletes.
const (
	ScaleDownSpread       = "spread"
	ScaleDownDeletionCost = "deletion-cost"
	ScaleDownRandom       = "random"
)

// ArmConfig selects the consolidation under test.
type ArmConfig struct {
	Name string `json:"name"`
	// Policy is the NodePool consolidation policy; baselines set it, pipeline arms default to
	// WhenEmptyOrUnderutilized.
	Policy string `json:"policy,omitempty"`
	// Pipeline is the pipeline config for the "pipeline" arm.
	Pipeline *pipeline.Config `json:"pipeline,omitempty"`
	// Descheduler configures B5.
	Descheduler *DeschedulerConfig `json:"descheduler,omitempty"`
}

// DeschedulerConfig is the HighNodeUtilization pattern: evict every pod from nodes whose CPU and memory requests are
// both below the threshold, so a MostAllocated scheduler repacks them and emptiness deletes the nodes.
type DeschedulerConfig struct {
	Threshold float64  `json:"threshold"`
	Interval  Duration `json:"interval"`
}

// LatencyConfig models the world's delays. Each is drawn uniformly from [Min, Max].
type LatencyConfig struct {
	Launch       Range `json:"launch"`       // cloud Create to instance running
	Registration Range `json:"registration"` // instance running to node ready and initialized
	PodStartup   Range `json:"podStartup"`   // bind to ready
	PodShutdown  Range `json:"podShutdown"`  // eviction to gone
}

// Range is a closed interval of durations.
type Range struct {
	Min Duration `json:"min"`
	Max Duration `json:"max"`
}

// Config is one simulation run.
type Config struct {
	RunID    string         `json:"runID"`
	Seed     int64          `json:"seed"`
	Scenario ScenarioConfig `json:"scenario"`
	Arm      ArmConfig      `json:"arm"`
	// Placement and ScaleDown pick the world models.
	Placement string `json:"placement"`
	ScaleDown string `json:"scaleDown"`
	// Duration is the simulated horizon; Warmup is how long the cluster runs before consolidation starts, so every
	// arm starts from the same provisioned state.
	Duration        Duration        `json:"duration"`
	Warmup          Duration        `json:"warmup"`
	MetricsInterval Duration        `json:"metricsInterval"`
	Catalog         catalog.Options `json:"catalog"`
	// ConsolidateAfter and Budget configure the single NodePool.
	ConsolidateAfter string        `json:"consolidateAfter"`
	Budget           string        `json:"budget"`
	Latency          LatencyConfig `json:"latency"`
	// MaxWall stops a run that exceeds this much wall time; the run is then recorded as timed out, not as data.
	MaxWall Duration `json:"maxWall,omitempty"`
}

// Defaults fills unset fields.
func (c *Config) Defaults() {
	if c.Placement == "" {
		c.Placement = LeastAllocated
	}
	if c.ScaleDown == "" {
		c.ScaleDown = ScaleDownSpread
	}
	if c.Duration.Duration == 0 {
		c.Duration = D(3 * time.Hour)
	}
	if c.Warmup.Duration == 0 {
		c.Warmup = D(10 * time.Minute)
	}
	if c.MetricsInterval.Duration == 0 {
		c.MetricsInterval = D(time.Minute)
	}
	if c.ConsolidateAfter == "" {
		c.ConsolidateAfter = "30s"
	}
	if c.Budget == "" {
		c.Budget = "10%"
	}
	if c.Catalog.MaxCPU == 0 {
		c.Catalog.MaxCPU = 32
	}
	l := &c.Latency
	def := func(r *Range, lo, hi time.Duration) {
		if r.Max.Duration == 0 {
			r.Min, r.Max = D(lo), D(hi)
		}
	}
	def(&l.Launch, 2*time.Second, 5*time.Second)
	def(&l.Registration, 30*time.Second, 60*time.Second)
	def(&l.PodStartup, 2*time.Second, 10*time.Second)
	def(&l.PodShutdown, 1*time.Second, 5*time.Second)
	if c.Arm.Name == ArmDescheduler && c.Arm.Descheduler == nil {
		c.Arm.Descheduler = &DeschedulerConfig{Threshold: 0.5, Interval: D(5 * time.Minute)}
	}
}

// Validate checks names.
func (c Config) Validate() error {
	switch c.Arm.Name {
	case ArmNone, ArmWhenEmpty, ArmUnderutil, ArmBalanced, ArmDescheduler:
	case ArmPipeline:
		if c.Arm.Pipeline == nil {
			return fmt.Errorf("arm %q needs a pipeline config", c.Arm.Name)
		}
	default:
		return fmt.Errorf("unknown arm %q", c.Arm.Name)
	}
	switch c.Placement {
	case LeastAllocated, MostAllocated, PlanFollowing:
	default:
		return fmt.Errorf("unknown placement model %q", c.Placement)
	}
	switch c.ScaleDown {
	case ScaleDownSpread, ScaleDownDeletionCost, ScaleDownRandom:
	default:
		return fmt.Errorf("unknown scale-down model %q", c.ScaleDown)
	}
	return nil
}
