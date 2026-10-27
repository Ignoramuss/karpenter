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

package main

import (
	"bufio"
	"compress/gzip"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"runtime/pprof"
	"strings"
	"time"

	"sigs.k8s.io/yaml"

	"sigs.k8s.io/karpenter/hack/consolidation-sim/sim"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/harness/catalog"
)

// Manifest records everything needed to reproduce a run (operating rule 7).
type Manifest struct {
	RunID       string     `json:"run_id"`
	GitSHA      string     `json:"git_sha"`
	GitDirty    bool       `json:"git_dirty"`
	Config      sim.Config `json:"config"`
	Seed        int64      `json:"seed"`
	Scenario    string     `json:"scenario"`
	Environment struct {
		GoVersion string `json:"go_version"`
		OS        string `json:"os"`
		Arch      string `json:"arch"`
		CPUs      int    `json:"cpus"`
		Hostname  string `json:"hostname"`
	} `json:"environment"`
	StartedAt  time.Time `json:"started_at"`
	FinishedAt time.Time `json:"finished_at"`
}

func gitInfo() (string, bool) {
	sha, err := exec.Command("git", "rev-parse", "HEAD").Output()
	if err != nil {
		return "unknown", false
	}
	st, _ := exec.Command("git", "status", "--porcelain", "--untracked-files=no").Output()
	return strings.TrimSpace(string(sha)), len(strings.TrimSpace(string(st))) > 0
}

func runCmd(args []string) error {
	fs := flag.NewFlagSet("run", flag.ExitOnError)
	config := fs.String("config", "", "cell config (YAML or JSON, sim.Config)")
	out := fs.String("out", "", "run directory to write")
	scenario := fs.String("scenario", "", "scenario, overrides config")
	nodes := fs.Int("nodes", 0, "scenario scale, overrides config")
	arm := fs.String("arm", "", "arm, overrides config")
	pipelineCfg := fs.String("pipeline", "", "pipeline config file for --arm pipeline")
	seed := fs.Int64("seed", 0, "seed, overrides config")
	duration := fs.Duration("duration", 0, "simulated horizon, overrides config")
	placement := fs.String("placement", "", "placement model, overrides config")
	scaleDown := fs.String("scale-down", "", "scale-down model, overrides config")
	maxWall := fs.Duration("max-wall", 0, "wall-time limit for the run")
	cpuProfile := fs.String("cpuprofile", "", "write a CPU profile here")
	_ = fs.Parse(args)
	if *cpuProfile != "" {
		f, err := os.Create(*cpuProfile)
		if err != nil {
			return err
		}
		if err := pprof.StartCPUProfile(f); err != nil {
			return err
		}
		defer pprof.StopCPUProfile()
	}
	var cfg sim.Config
	if *config != "" {
		raw, err := os.ReadFile(*config)
		if err != nil {
			return err
		}
		if err := yaml.UnmarshalStrict(raw, &cfg); err != nil {
			return fmt.Errorf("parsing %s, %w", *config, err)
		}
	}
	if *scenario != "" {
		cfg.Scenario.Name = *scenario
	}
	if *nodes > 0 {
		cfg.Scenario.Nodes = *nodes
	}
	if *arm != "" {
		cfg.Arm.Name = *arm
	}
	if *pipelineCfg != "" {
		pc, err := pipeline.LoadConfig(*pipelineCfg)
		if err != nil {
			return err
		}
		cfg.Arm.Pipeline = &pc
	}
	if *seed != 0 {
		cfg.Seed = *seed
	}
	if *duration > 0 {
		cfg.Duration = sim.D(*duration)
	}
	if *placement != "" {
		cfg.Placement = *placement
	}
	if *scaleDown != "" {
		cfg.ScaleDown = *scaleDown
	}
	if *maxWall > 0 {
		cfg.MaxWall = sim.D(*maxWall)
	}
	if *out == "" {
		return fmt.Errorf("--out is required")
	}
	if cfg.RunID == "" {
		cfg.RunID = filepath.Base(*out)
	}
	_, err := RunCell(cfg, *out)
	return err
}

// RunCell runs one configuration into dir: manifest.json, decisions.jsonl.gz, timeseries.csv, podsets.jsonl and
// summary.json. The decision log is gzipped (with a zero header timestamp, so identical logs compress to identical
// bytes): pipeline arms log every generated move, tens of megabytes per run before compression.
func RunCell(cfg sim.Config, dir string) (sim.Summary, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return sim.Summary{}, err
	}
	cfg.Defaults()
	man := Manifest{RunID: cfg.RunID, Config: cfg, Seed: cfg.Seed, Scenario: cfg.Scenario.Name, StartedAt: time.Now().UTC()}
	man.GitSHA, man.GitDirty = gitInfo()
	man.Environment.GoVersion, man.Environment.OS, man.Environment.Arch, man.Environment.CPUs = runtime.Version(), runtime.GOOS, runtime.GOARCH, runtime.NumCPU()
	man.Environment.Hostname, _ = os.Hostname()
	dec, err := os.Create(filepath.Join(dir, "decisions.jsonl.gz"))
	if err != nil {
		return sim.Summary{}, err
	}
	defer dec.Close()
	gz := gzip.NewWriter(dec)
	bw := bufio.NewWriter(gz)
	s, err := sim.New(cfg, bw)
	if err != nil {
		return sim.Summary{}, err
	}
	ts, err := os.Create(filepath.Join(dir, "timeseries.csv"))
	if err != nil {
		return sim.Summary{}, err
	}
	defer ts.Close()
	sum, runErr := s.Run(ts)
	if err := bw.Flush(); err != nil {
		return sum, err
	}
	if err := gz.Close(); err != nil {
		return sum, err
	}
	ps, err := os.Create(filepath.Join(dir, "podsets.jsonl"))
	if err != nil {
		return sum, err
	}
	defer ps.Close()
	if err := s.WritePodSets(ps); err != nil {
		return sum, err
	}
	man.FinishedAt = time.Now().UTC()
	if err := writeJSON(filepath.Join(dir, "manifest.json"), man); err != nil {
		return sum, err
	}
	if err := writeJSON(filepath.Join(dir, "summary.json"), sum); err != nil {
		return sum, err
	}
	return sum, runErr
}

func writeJSON(path string, v any) error {
	raw, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(raw, '\n'), 0o644)
}

// catalogEntry is one instance type as the offline packing bound (B6) sees it: allocatable resources and price.
type catalogEntry struct {
	Name      string  `json:"name"`
	Family    string  `json:"family"`
	CPU       int64   `json:"cpu_millicores"`
	MemoryMiB int64   `json:"memory_mib"`
	Pods      int64   `json:"pods"`
	Price     float64 `json:"price_per_hour"`
}

// catalogCmd prints the instance types a run with the default catalog options can launch.
func catalogCmd(args []string) error {
	fs := flag.NewFlagSet("catalog", flag.ExitOnError)
	maxCPU := fs.Int64("max-cpu", 0, "drop types above this many vCPUs (default: the simulator's)")
	_ = fs.Parse(args)
	cfg := sim.Config{}
	cfg.Catalog.MaxCPU = *maxCPU
	cfg.Defaults()
	cat, err := catalog.Load(cfg.Catalog)
	if err != nil {
		return err
	}
	out := make([]catalogEntry, 0, len(cat.Entries))
	for _, e := range cat.Entries {
		alloc := e.InstanceType.Allocatable()
		out = append(out, catalogEntry{Name: e.InstanceType.Name, Family: e.Family, CPU: alloc.Cpu().MilliValue(),
			MemoryMiB: alloc.Memory().Value() >> 20, Pods: alloc.Pods().Value(), Price: e.Price})
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(out)
}

func strategiesCmd() error {
	return json.NewEncoder(os.Stdout).Encode(pipeline.Registered())
}

func matrixCmd(args []string) error { return runMatrix(args) }
