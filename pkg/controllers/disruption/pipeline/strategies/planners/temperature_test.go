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

package planners

import (
	"testing"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
)

func TestTemperatureUnits(t *testing.T) {
	var nodes []pipeline.NodeView
	for _, name := range []string{"a", "b", "c", "d"} {
		nodes = append(nodes, pipeline.NodeView{Name: name, NodePool: "default", Price: 1, Capacity: pipeline.Resources{pipeline.CPU: 1}})
	}
	s := pipeline.NewState(nodes, nil, nil, nil)
	if got := temperature(0.2, TemperatureAbsolute, s); got != 0.2 {
		t.Errorf("absolute temperature %v, want 0.2", got)
	}
	if got := temperature(0.2, TemperaturePerNode, s); got != 0.05 {
		t.Errorf("per-node temperature %v, want 0.2 / 4 nodes", got)
	}
	for _, name := range []string{AnnealPlanName, AnnealOnlineName} {
		for unit, ok := range map[string]bool{"": true, TemperatureAbsolute: true, TemperaturePerNode: true, "kelvin": false} {
			_, err := pipeline.NewPlanner(pipeline.SyntheticEnv(nil), pipeline.StageConfig{Name: name, Params: pipeline.Params{"temperatureUnit": unit}})
			if (err == nil) != ok {
				t.Errorf("%s with temperatureUnit %q: error %v, want ok %v", name, unit, err, ok)
			}
		}
	}
}
