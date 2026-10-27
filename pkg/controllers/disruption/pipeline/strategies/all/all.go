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

// Package all registers every consolidation pipeline strategy. Import it for its side effects wherever a pipeline
// config may name a strategy outside package pipeline (the controller, the simulator, tests).
package all

import (
	// Stage 1 selectors: uniform-random, util-band, potential-weighted.
	_ "sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/strategies/candidates"
	// Stage 2 generators: random-merge, random-shift, greedy-shift, swap.
	_ "sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/strategies/moves"
	// Stage 3 planners: entropy-greedy, anneal-plan, anneal-online, sequence.
	_ "sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/strategies/planners"
)
