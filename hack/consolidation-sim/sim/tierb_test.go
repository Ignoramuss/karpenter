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
	"io"
	"math/rand"
	"reflect"
	"testing"
	"time"
)

// WB must be W2 for the same n and seed, deployment by deployment, except that every drop happens at dropAt.
func TestTierBIsW2WithOneDrop(t *testing.T) {
	for _, n := range []int{50, 200} {
		for seed := int64(101); seed <= 103; seed++ {
			w2, err := Generate(ScenarioConfig{Name: W2Diurnal, Nodes: n}, rand.New(rand.NewSource(seed)), testTraceCatalog) //nolint:gosec
			if err != nil {
				t.Fatal(err)
			}
			wb, err := Generate(ScenarioConfig{Name: WBTierB, Nodes: n, Param: map[string]any{"dropAt": "7m"}},
				rand.New(rand.NewSource(seed)), testTraceCatalog) //nolint:gosec
			if err != nil {
				t.Fatal(err)
			}
			if wb.Name != WBTierB || len(wb.Deployments) != len(w2.Deployments) {
				t.Fatalf("n=%d seed=%d: WB %q has %d deployments, W2 %d", n, seed, wb.Name, len(wb.Deployments), len(w2.Deployments))
			}
			for i, d := range wb.Deployments {
				want := w2.Deployments[i]
				want.Schedule = []ReplicaPoint{want.Schedule[0], {At: D(7 * time.Minute), Replicas: want.Schedule[1].Replicas}}
				if !reflect.DeepEqual(d, want) {
					t.Errorf("n=%d seed=%d deployment %d: got %+v, want %+v", n, seed, i, d, want)
				}
			}
		}
	}
}

func TestTierBDropAt(t *testing.T) {
	wb, err := Generate(ScenarioConfig{Name: WBTierB, Nodes: 50}, rand.New(rand.NewSource(1)), testTraceCatalog) //nolint:gosec
	if err != nil {
		t.Fatal(err)
	}
	for _, d := range wb.Deployments {
		if got := d.Schedule[1].At.Duration; got != 5*time.Minute {
			t.Fatalf("default drop at %s, want 5m", got)
		}
	}
	if _, err := Generate(ScenarioConfig{Name: WBTierB, Nodes: 50, Param: map[string]any{"dropAt": "soon"}},
		rand.New(rand.NewSource(1)), testTraceCatalog); err == nil { //nolint:gosec
		t.Fatal("a malformed dropAt was accepted")
	}
}

// GenerateWorkload must return exactly the workload New simulates, so Tier B replays what Tier A ran.
func TestGenerateWorkloadMatchesRun(t *testing.T) {
	cfg := Config{Seed: 101, Scenario: ScenarioConfig{Name: WBTierB, Nodes: 50}, Arm: ArmConfig{Name: ArmUnderutil}}
	w, err := GenerateWorkload(cfg)
	if err != nil {
		t.Fatal(err)
	}
	s, err := New(cfg, io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(w, s.work) {
		t.Fatalf("GenerateWorkload differs from the run's workload:\n%+v\n%+v", w, s.work)
	}
}
