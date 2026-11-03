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

package executors

import (
	"context"
	"fmt"
	"sort"
	"time"

	"github.com/samber/lo"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/utils/clock"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
)

// shiftSourceTaint is PreferNoSchedule, not NoSchedule: the node stays a valid home for pods that fit nowhere else,
// and nothing else may treat it as disrupted.
var shiftSourceTaint = corev1.Taint{Key: pipeline.ShiftSourceTaintKey, Effect: corev1.TaintEffectPreferNoSchedule}

// conflictRetries bounds re-reads after an optimistic-lock conflict. Retrying at once rather than backing off keeps
// executors off the wall clock; another writer racing the same node is rare.
const conflictRetries = 3

func (e *executor) clock() clock.Clock {
	if e.env.Clock != nil {
		return e.env.Clock
	}
	return e.env.Upstream.Clock
}

func hasShiftTaint(n *corev1.Node) (exact, keyed bool) {
	for _, t := range n.Spec.Taints {
		if t.Key == pipeline.ShiftSourceTaintKey {
			keyed = true
			exact = exact || t.MatchTaint(&shiftSourceTaint)
		}
	}
	return exact, keyed
}

func withoutShiftTaint(ts []corev1.Taint) []corev1.Taint {
	return lo.Reject(ts, func(t corev1.Taint, _ int) bool { return t.Key == pipeline.ShiftSourceTaintKey })
}

// markSources taints a shift's sources, in name order, when tainting is on, and returns the nodes it changed. Sources
// that are also destinations in this plan are left alone, since the taint would push the plan's own pods away.
func (e *executor) markSources(ctx context.Context, x *shiftRun, m pipeline.Move) ([]string, error) {
	if !e.taint {
		return nil, nil
	}
	var changed []string
	for _, name := range shiftSources(x, m) {
		ok, err := e.patchNode(ctx, name, e.markSource)
		if err != nil {
			e.unmarkSources(ctx, changed)
			return nil, fmt.Errorf("tainting %s, %w", name, err)
		}
		if ok {
			changed = append(changed, name)
		}
	}
	return changed, nil
}

// markSource adds the taint and stamps when it went on. A node that already carries both is unchanged, so the
// TTL runs from when the node first became a source, not from its latest shift; once the taint lapses, the next
// shift from the node places a fresh one.
func (e *executor) markSource(n *corev1.Node) bool {
	if !n.DeletionTimestamp.IsZero() {
		return false
	}
	exact, _ := hasShiftTaint(n)
	if _, stamped := n.Annotations[ShiftSourceSinceAnnotationKey]; exact && stamped {
		return false
	}
	n.Spec.Taints = append(withoutShiftTaint(n.Spec.Taints), shiftSourceTaint)
	if n.Annotations == nil {
		n.Annotations = map[string]string{}
	}
	n.Annotations[ShiftSourceSinceAnnotationKey] = e.clock().Now().UTC().Format(time.RFC3339)
	return true
}

// unmarkSources takes off taints this Execute placed, best effort: a taint left behind only lapses at its TTL.
func (e *executor) unmarkSources(ctx context.Context, names []string) {
	for _, name := range names {
		if _, err := e.patchNode(ctx, name, clearShiftSource); err != nil && !apierrors.IsNotFound(err) {
			log.FromContext(ctx).V(1).Info("failed removing shift-source taint", "node", name, "error", err.Error())
		}
	}
}

func clearShiftSource(n *corev1.Node) bool {
	_, keyed := hasShiftTaint(n)
	_, stamped := n.Annotations[ShiftSourceSinceAnnotationKey]
	if !keyed && !stamped {
		return false
	}
	n.Spec.Taints = withoutShiftTaint(n.Spec.Taints)
	delete(n.Annotations, ShiftSourceSinceAnnotationKey)
	return true
}

// expired reports a shift-source mark older than ttl. A taint with no readable stamp counts as expired, or it would
// never lapse.
func expired(n *corev1.Node, now time.Time, ttl time.Duration) bool {
	_, keyed := hasShiftTaint(n)
	v, stamped := n.Annotations[ShiftSourceSinceAnnotationKey]
	if !keyed && !stamped {
		return false
	}
	since, err := time.Parse(time.RFC3339, v)
	return !stamped || err != nil || now.Sub(since) > ttl
}

// expireTaints lifts shift-source marks older than the TTL, in node-name order. Failures are logged rather than
// returned: a mark that outlives its TTL is only a scheduling preference, and the next Execute retries it.
func (e *executor) expireTaints(ctx context.Context) {
	nodes := &corev1.NodeList{}
	if err := e.env.Upstream.KubeClient.List(ctx, nodes); err != nil {
		log.FromContext(ctx).V(1).Info("failed listing nodes to expire shift-source taints", "error", err.Error())
		return
	}
	sort.Slice(nodes.Items, func(i, j int) bool { return nodes.Items[i].Name < nodes.Items[j].Name })
	now := e.clock().Now()
	for i := range nodes.Items {
		if !expired(&nodes.Items[i], now, e.taintTTL) {
			continue
		}
		name := nodes.Items[i].Name
		// Re-check on the node as fetched for the patch: a shift may have re-marked it since the list.
		_, err := e.patchNode(ctx, name, func(n *corev1.Node) bool {
			return n.DeletionTimestamp.IsZero() && expired(n, now, e.taintTTL) && clearShiftSource(n)
		})
		if err != nil && !apierrors.IsNotFound(err) {
			log.FromContext(ctx).V(1).Info("failed expiring shift-source taint", "node", name, "error", err.Error())
		}
	}
}

// patchNode applies mutate to the current node and patches the difference under an optimistic lock, since a merge
// patch replaces the whole taint list. It reports whether mutate changed the node.
func (e *executor) patchNode(ctx context.Context, name string, mutate func(*corev1.Node) bool) (bool, error) {
	kube := e.env.Upstream.KubeClient
	var err error
	for range conflictRetries {
		node := &corev1.Node{}
		if err = kube.Get(ctx, client.ObjectKey{Name: name}, node); err != nil {
			return false, err
		}
		stored := node.DeepCopy()
		if !mutate(node) {
			return false, nil
		}
		if err = kube.Patch(ctx, node, client.MergeFromWithOptions(stored, client.MergeFromWithOptimisticLock{})); err == nil {
			return true, nil
		}
		if !apierrors.IsConflict(err) {
			return false, err
		}
	}
	return false, err
}
