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

package disruption

import (
	"context"

	corev1 "k8s.io/api/core/v1"

	"sigs.k8s.io/karpenter/pkg/cloudprovider"
)

// This file exposes consolidation's building blocks to the consolidation pipeline (pkg/controllers/disruption/pipeline,
// behind the ConsolidationPipeline feature gate), whose upstream adapters wrap them to reproduce these methods'
// decisions stage by stage. Nothing here changes behavior.

// ReschedulablePods returns the candidate's pods that would move if it were disrupted.
func (c *Candidate) ReschedulablePods() []*corev1.Pod { return c.reschedulablePods }

// InstanceType returns the candidate's instance type, nil if the NodePool no longer offers it.
func (c *Candidate) InstanceType() *cloudprovider.InstanceType { return c.instanceType }

// Zone returns the candidate's zone label.
func (c *Candidate) Zone() string { return c.zone }

// CapacityType returns the candidate's capacity type label.
func (c *Candidate) CapacityType() string { return c.capacityType }

// ComputeConsolidation simulates disrupting the candidates together and returns the delete or replace command that
// would follow, or a no-op command.
func (c *consolidation) ComputeConsolidation(ctx context.Context, candidates ...*Candidate) (Command, error) {
	return c.computeConsolidation(ctx, candidates...)
}

// SortBySavingsRatio applies the consolidation candidate order (savings ratio, descending) in place and returns it.
func (c *consolidation) SortBySavingsRatio(ctx context.Context, candidates []*Candidate) []*Candidate {
	return c.sortCandidates(ctx, candidates)
}

// MarkConsolidated records that nothing more can be consolidated in the current cluster state.
func (c *consolidation) MarkConsolidated() { c.markConsolidated() }

// FilterOutSameInstanceType drops replacement instance types that cost at least as much as the cheapest source of
// the same type, as multi-node consolidation does before accepting a replace.
func FilterOutSameInstanceType(replacement *Replacement, consolidate []*Candidate) (*Replacement, error) {
	return filterOutSameInstanceType(replacement, consolidate)
}
