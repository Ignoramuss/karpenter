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
	"context"

	"github.com/go-logr/logr"
	"k8s.io/utils/clock"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	"sigs.k8s.io/karpenter/pkg/cloudprovider"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption"
	"sigs.k8s.io/karpenter/pkg/controllers/provisioning"
	"sigs.k8s.io/karpenter/pkg/controllers/state"
	"sigs.k8s.io/karpenter/pkg/events"
	"sigs.k8s.io/karpenter/pkg/operator/options"
	"sigs.k8s.io/karpenter/pkg/state/cost"
)

// Methods returns the disruption controller's default methods with multi-node and single-node consolidation replaced
// by one PipelineConsolidation method, configured from --consolidation-pipeline-config. Every other method keeps its
// place and behavior.
func Methods(ctx context.Context, clk clock.Clock, kubeClient client.Client, provisioner *provisioning.Provisioner,
	cp cloudprovider.CloudProvider, recorder events.Recorder, cluster *state.Cluster, queue *disruption.Queue, clusterCost *cost.ClusterCost) ([]disruption.Method, error) {
	cfg, err := LoadConfig(options.FromContext(ctx).ConsolidationPipelineConfig)
	if err != nil {
		return nil, err
	}
	m, err := NewMethod(clk, kubeClient, cluster, provisioner, cp, recorder, queue, clusterCost, cfg, MethodOptions{
		RunID:    "controller",
		Recorder: LogRecorder{Logger: log.FromContext(ctx).WithName("consolidation-pipeline")},
	})
	if err != nil {
		return nil, err
	}
	return ReplaceConsolidation(disruption.NewMethods(ctx, clk, cluster, kubeClient, provisioner, cp, recorder, queue), m), nil
}

// ReplaceConsolidation puts m where multi-node consolidation was and drops single-node consolidation.
func ReplaceConsolidation(methods []disruption.Method, m disruption.Method) []disruption.Method {
	out := make([]disruption.Method, 0, len(methods))
	placed := false
	for _, method := range methods {
		switch method.(type) {
		case *disruption.MultiNodeConsolidation, *disruption.SingleNodeConsolidation:
			if !placed {
				out = append(out, m)
				placed = true
			}
		default:
			out = append(out, method)
		}
	}
	if !placed {
		out = append(out, m)
	}
	return out
}

// LogRecorder writes decision events to the controller's structured logs at debug verbosity, so a cluster run's
// decision log can be collected from controller logs.
type LogRecorder struct{ Logger logr.Logger }

func (l LogRecorder) Record(e DecisionEvent) {
	l.Logger.V(1).Info("consolidation decision", "schema_version", e.SchemaVersion, "run_id", e.RunID, "cycle", e.Cycle, "t", e.T,
		"stage", e.Stage, "strategy", e.Strategy, "move_id", e.MoveID, "kind", e.Kind, "sources", e.Sources,
		"destinations", e.Destinations, "pods", e.Pods, "delta_potential", e.DeltaPotential, "disruption", e.Disruption,
		"savings_per_hour", e.SavingsPerHour, "accepted", e.Accepted, "reason", e.Reason, "budget_remaining", e.BudgetRemaining,
		"detail", e.Detail)
}
