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
	"sort"
	"sync"

	"k8s.io/utils/clock"
)

// Env is what strategies are constructed with: the injected clock, the shared services, and, inside a live
// controller or the simulator, handles on upstream Karpenter for the adapters and for Full feasibility.
type Env struct {
	Clock       clock.Clock
	Potential   Potential
	Feasibility Feasibility
	Scorer      Scorer
	Recorder    Recorder
	// Upstream is nil for synthetic snapshots (unit and conformance tests), where only snapshot-level strategies run.
	Upstream *Upstream
	// Observer, when set, sees each move an executor is about to carry out (harnesses use it for plan fidelity).
	Observer func(Move)
	// Steer, when set, receives an executor's request that the pod replacing an evicted pod prefer a node: what a
	// mutating webhook adding preferred node affinity would do. The simulator implements it; a real cluster needs
	// the webhook, which is experiment-only.
	Steer func(pod PodKey, node string)
}

// Observe reports a move to the observer, if any.
func (e *Env) Observe(m Move) {
	if e.Observer != nil {
		e.Observer(m)
	}
}

// Params are a strategy's raw config parameters.
type Params map[string]any

// Decode strictly decodes params into out, rejecting unknown keys so a typo in a run config fails loudly.
func (p Params) Decode(out any) error {
	if len(p) == 0 {
		return nil
	}
	raw, err := json.Marshal(p)
	if err != nil {
		return err
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	return dec.Decode(out)
}

type (
	CandidateSelectorFactory func(env *Env, p Params) (CandidateSelector, error)
	MoveGeneratorFactory     func(env *Env, p Params) (MoveGenerator, error)
	PlannerFactory           func(env *Env, p Params) (Planner, error)
	ExecutorFactory          func(env *Env, p Params) (Executor, error)
	PotentialFactory         func(p Params) (Potential, error)
)

var registry = struct {
	sync.RWMutex
	candidates map[string]CandidateSelectorFactory
	moves      map[string]MoveGeneratorFactory
	planners   map[string]PlannerFactory
	executors  map[string]ExecutorFactory
	potentials map[string]PotentialFactory
}{
	candidates: map[string]CandidateSelectorFactory{},
	moves:      map[string]MoveGeneratorFactory{},
	planners:   map[string]PlannerFactory{},
	executors:  map[string]ExecutorFactory{},
	potentials: map[string]PotentialFactory{},
}

func register[F any](m map[string]F, kind, name string, f F) {
	registry.Lock()
	defer registry.Unlock()
	if _, dup := m[name]; dup {
		panic(fmt.Sprintf("pipeline: %s %q registered twice", kind, name))
	}
	m[name] = f
}

func lookup[F any](m map[string]F, kind, name string) (F, error) {
	registry.RLock()
	defer registry.RUnlock()
	f, ok := m[name]
	if !ok {
		var zero F
		return zero, fmt.Errorf("pipeline: unknown %s %q (registered: %v)", kind, name, sortedKeys(m))
	}
	return f, nil
}

func sortedKeys[F any](m map[string]F) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func RegisterCandidateSelector(name string, f CandidateSelectorFactory) {
	register(registry.candidates, "candidate selector", name, f)
}
func RegisterMoveGenerator(name string, f MoveGeneratorFactory) {
	register(registry.moves, "move generator", name, f)
}
func RegisterPlanner(name string, f PlannerFactory) { register(registry.planners, "planner", name, f) }
func RegisterExecutor(name string, f ExecutorFactory) {
	register(registry.executors, "executor", name, f)
}
func RegisterPotential(name string, f PotentialFactory) {
	register(registry.potentials, "potential", name, f)
}

func NewCandidateSelector(env *Env, c StageConfig) (CandidateSelector, error) {
	f, err := lookup(registry.candidates, "candidate selector", c.Name)
	if err != nil {
		return nil, err
	}
	return f(env, c.Params)
}

func NewMoveGenerator(env *Env, c StageConfig) (MoveGenerator, error) {
	f, err := lookup(registry.moves, "move generator", c.Name)
	if err != nil {
		return nil, err
	}
	return f(env, c.Params)
}

func NewPlanner(env *Env, c StageConfig) (Planner, error) {
	f, err := lookup(registry.planners, "planner", c.Name)
	if err != nil {
		return nil, err
	}
	return f(env, c.Params)
}

func NewExecutor(env *Env, c StageConfig) (Executor, error) {
	f, err := lookup(registry.executors, "executor", c.Name)
	if err != nil {
		return nil, err
	}
	return f(env, c.Params)
}

func NewPotential(c StageConfig) (Potential, error) {
	f, err := lookup(registry.potentials, "potential", c.Name)
	if err != nil {
		return nil, err
	}
	return f(c.Params)
}

// Registered lists every registered strategy name per stage, for docs and CLI help.
func Registered() map[string][]string {
	registry.RLock()
	defer registry.RUnlock()
	return map[string][]string{
		StageCandidates: sortedKeys(registry.candidates),
		StageMoves:      sortedKeys(registry.moves),
		StagePlan:       sortedKeys(registry.planners),
		StageExecute:    sortedKeys(registry.executors),
		"potential":     sortedKeys(registry.potentials),
	}
}

func init() {
	potential := func(ctor func(PotentialParams) (Potential, error)) PotentialFactory {
		return func(p Params) (Potential, error) {
			var pp PotentialParams
			if err := p.Decode(&pp); err != nil {
				return nil, err
			}
			return ctor(pp)
		}
	}
	RegisterPotential("load-entropy", potential(NewLoadEntropy))
	RegisterPotential("util-entropy", potential(NewUtilEntropy))
	RegisterPotential("util-quadratic", potential(NewUtilQuadratic))
	RegisterPotential("none", func(Params) (Potential, error) { return nonePotential{}, nil })
}
