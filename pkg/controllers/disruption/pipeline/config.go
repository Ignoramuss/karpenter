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

package pipeline

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"

	"sigs.k8s.io/yaml"
)

// StageConfig selects one strategy by name.
type StageConfig struct {
	Name   string `json:"name"`
	Params Params `json:"params,omitempty"`
}

// StageList is one or more strategies; config accepts a single object or a list.
type StageList []StageConfig

func (l *StageList) UnmarshalJSON(b []byte) error {
	b = bytes.TrimSpace(b)
	if len(b) > 0 && b[0] == '{' {
		var one StageConfig
		if err := json.Unmarshal(b, &one); err != nil {
			return err
		}
		*l = StageList{one}
		return nil
	}
	var many []StageConfig
	if err := json.Unmarshal(b, &many); err != nil {
		return err
	}
	*l = many
	return nil
}

// MovesMode says how several move generators combine.
type MovesMode string

const (
	// Sequential runs generators in order as separate rounds, each planned and executed on its own; the first round
	// that executes a move ends the cycle. This is how the disruption controller runs multi-node then single-node
	// consolidation, so the upstream configuration uses it.
	Sequential MovesMode = "sequential"
	// Pooled runs every generator against one shared search budget and plans over all their moves at once.
	Pooled MovesMode = "pooled"
)

// SearchConfig caps per-cycle work.
type SearchConfig struct {
	MaxAttempts        int `json:"maxAttempts,omitempty"`
	MaxFullSimulations int `json:"maxFullSimulations,omitempty"`
	// Timeout bounds a cycle's search on the injected clock, e.g. "1m". Empty means no pipeline-level deadline; the
	// upstream adapters keep their own 1 and 3 minute limits.
	Timeout string `json:"timeout,omitempty"`
}

func (s SearchConfig) timeout() (time.Duration, error) {
	if s.Timeout == "" {
		return 0, nil
	}
	return time.ParseDuration(s.Timeout)
}

// BudgetConfig adds pod-eviction budgets to the NodePool node budgets, which shift moves cannot see.
type BudgetConfig struct {
	// PodEvictionsPerMinute caps evictions per NodePool per minute; 0 means unlimited.
	PodEvictionsPerMinute int `json:"podEvictionsPerMinute,omitempty"`
	// WorkloadEvictionsPerMinute caps evictions per workload (controlling owner) per minute; 0 means unlimited.
	WorkloadEvictionsPerMinute int `json:"workloadEvictionsPerMinute,omitempty"`
}

// Config is one pipeline: a strategy per stage, the shared potential, the seed, and per-cycle limits.
type Config struct {
	Candidates StageConfig  `json:"candidates"`
	Moves      StageList    `json:"moves"`
	MovesMode  MovesMode    `json:"movesMode,omitempty"`
	Planner    StageConfig  `json:"planner"`
	Executor   StageConfig  `json:"executor"`
	Potential  StageConfig  `json:"potential"`
	Seed       int64        `json:"seed"`
	Search     SearchConfig `json:"search,omitempty"`
	Budgets    BudgetConfig `json:"budgets,omitempty"`
}

// DefaultConfig reproduces upstream consolidation: upstream candidates, multi-node then single-node consolidation as
// sequential rounds, the upstream planner (budgets, Balanced scoring where a NodePool asks for it, first valid
// command), and the orchestration queue.
func DefaultConfig() Config {
	return Config{
		Candidates: StageConfig{Name: "upstream"},
		Moves:      StageList{{Name: "upstream-multi"}, {Name: "upstream-single"}},
		MovesMode:  Sequential,
		Planner:    StageConfig{Name: "upstream"},
		Executor:   StageConfig{Name: "upstream-queue"},
		Potential:  StageConfig{Name: "none"},
	}
}

func (c *Config) defaults() {
	d := DefaultConfig()
	if c.Candidates.Name == "" {
		c.Candidates = d.Candidates
	}
	if len(c.Moves) == 0 {
		c.Moves = d.Moves
	}
	if c.MovesMode == "" {
		c.MovesMode = Sequential
	}
	if c.Planner.Name == "" {
		c.Planner = d.Planner
	}
	if c.Executor.Name == "" {
		c.Executor = d.Executor
	}
	if c.Potential.Name == "" {
		c.Potential = d.Potential
	}
}

// Validate checks the config names registered strategies and sane limits.
//
//nolint:gocyclo
func (c Config) Validate() error {
	if c.MovesMode != Sequential && c.MovesMode != Pooled {
		return fmt.Errorf("movesMode %q, want %s or %s", c.MovesMode, Sequential, Pooled)
	}
	if _, err := c.Search.timeout(); err != nil {
		return fmt.Errorf("search.timeout, %w", err)
	}
	if c.Search.MaxAttempts < 0 || c.Search.MaxFullSimulations < 0 {
		return fmt.Errorf("search limits must be non-negative")
	}
	if c.Budgets.PodEvictionsPerMinute < 0 || c.Budgets.WorkloadEvictionsPerMinute < 0 {
		return fmt.Errorf("eviction budgets must be non-negative")
	}
	reg := Registered()
	check := func(stage string, name string) error {
		for _, n := range reg[stage] {
			if n == name {
				return nil
			}
		}
		return fmt.Errorf("unknown %s strategy %q (registered: %v)", stage, name, reg[stage])
	}
	if err := check(StageCandidates, c.Candidates.Name); err != nil {
		return err
	}
	for _, m := range c.Moves {
		if err := check(StageMoves, m.Name); err != nil {
			return err
		}
	}
	if err := check(StagePlan, c.Planner.Name); err != nil {
		return err
	}
	if err := check(StageExecute, c.Executor.Name); err != nil {
		return err
	}
	return check("potential", c.Potential.Name)
}

// ParseConfig reads YAML or JSON, with or without a top-level "pipeline:" key, fills defaults and validates.
func ParseConfig(raw []byte) (Config, error) {
	var wrapped struct {
		Pipeline *Config `json:"pipeline"`
	}
	var cfg Config
	if err := yaml.Unmarshal(raw, &wrapped); err == nil && wrapped.Pipeline != nil {
		cfg = *wrapped.Pipeline
	} else if err := yaml.UnmarshalStrict(raw, &cfg); err != nil {
		return Config{}, fmt.Errorf("parsing pipeline config, %w", err)
	}
	cfg.defaults()
	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

// LoadConfig reads a config file; an empty path returns DefaultConfig. A value that is itself a YAML or JSON
// document (it starts with "{" or "pipeline:", or spans lines) is parsed inline, so the config can travel in an
// environment variable.
func LoadConfig(path string) (Config, error) {
	if path == "" {
		cfg := DefaultConfig()
		return cfg, cfg.Validate()
	}
	if trimmed := strings.TrimSpace(path); strings.HasPrefix(trimmed, "{") || strings.HasPrefix(trimmed, "pipeline:") || strings.Contains(trimmed, "\n") {
		return ParseConfig([]byte(path))
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return Config{}, fmt.Errorf("reading pipeline config %s, %w", path, err)
	}
	return ParseConfig(raw)
}
