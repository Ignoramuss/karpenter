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

package harness

import (
	"sort"
	"sync"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// podIndex serves pod lists selected only by spec.nodeName (and optionally namespace). The fake client answers an
// indexed list by deep copying every object of the type and then filtering, which made per-node pod lists the
// dominant cost of candidate construction; Karpenter issues one per node per cycle. The index holds the latest
// stored copy of each pod, refreshed after every pod write.
type podIndex struct {
	mu     sync.RWMutex
	byNode map[string]map[types.NamespacedName]*corev1.Pod
	nodeOf map[types.NamespacedName]string
}

func newPodIndex() *podIndex {
	return &podIndex{byNode: map[string]map[types.NamespacedName]*corev1.Pod{}, nodeOf: map[types.NamespacedName]string{}}
}

func (p *podIndex) put(pod *corev1.Pod) {
	p.mu.Lock()
	defer p.mu.Unlock()
	key := client.ObjectKeyFromObject(pod)
	p.removeLocked(key)
	m, ok := p.byNode[pod.Spec.NodeName]
	if !ok {
		m = map[types.NamespacedName]*corev1.Pod{}
		p.byNode[pod.Spec.NodeName] = m
	}
	m[key] = pod
	p.nodeOf[key] = pod.Spec.NodeName
}

func (p *podIndex) remove(key types.NamespacedName) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.removeLocked(key)
}

func (p *podIndex) removeLocked(key types.NamespacedName) {
	if node, ok := p.nodeOf[key]; ok {
		delete(p.byNode[node], key)
		delete(p.nodeOf, key)
	}
}

// list returns the pods on a node in namespace/name order, the order the fake tracker lists in.
func (p *podIndex) list(nodeName, namespace string, deepCopy bool) []corev1.Pod {
	p.mu.RLock()
	defer p.mu.RUnlock()
	m := p.byNode[nodeName]
	keys := make([]types.NamespacedName, 0, len(m))
	for k := range m {
		if namespace == "" || k.Namespace == namespace {
			keys = append(keys, k)
		}
	}
	sort.Slice(keys, func(i, j int) bool {
		if keys[i].Namespace != keys[j].Namespace {
			return keys[i].Namespace < keys[j].Namespace
		}
		return keys[i].Name < keys[j].Name
	})
	out := make([]corev1.Pod, len(keys))
	for i, k := range keys {
		if deepCopy {
			m[k].DeepCopyInto(&out[i])
		} else {
			out[i] = *m[k]
		}
	}
	return out
}

// Count returns how many pods the index holds, for consistency checks.
func (p *podIndex) count() int {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return len(p.nodeOf)
}
