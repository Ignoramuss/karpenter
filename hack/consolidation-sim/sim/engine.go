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

// Package sim is the Tier A consolidation simulator: real Karpenter (cluster state, provisioner, disruption methods
// or the consolidation pipeline, orchestration queue) on an in-memory API and a virtual clock, driven by modeled
// kube-scheduler placement, ReplicaSet scale-down, cloud launch and drain latencies, and a workload. One run is
// single-threaded and every random draw comes from seeded streams, so a run is reproducible byte for byte.
package sim

import (
	"container/heap"
	"time"
)

type event struct {
	at   time.Time
	seq  int
	name string
	fn   func()
	// deferWhileDisrupting events wait until a disruption pass finishes, the way the singleton disruption controller
	// cannot start its next reconcile while one is in flight (waiting out validation).
	deferWhileDisrupting bool
}

type eventHeap []*event

func (h eventHeap) Len() int { return len(h) }
func (h eventHeap) Less(i, j int) bool {
	if !h[i].at.Equal(h[j].at) {
		return h[i].at.Before(h[j].at)
	}
	return h[i].seq < h[j].seq
}
func (h eventHeap) Swap(i, j int) { h[i], h[j] = h[j], h[i] }
func (h *eventHeap) Push(x any)   { *h = append(*h, x.(*event)) }
func (h *eventHeap) Pop() any {
	old := *h
	n := len(old)
	e := old[n-1]
	*h = old[:n-1]
	return e
}

// at schedules fn at t; events at the same time run in scheduling order.
func (s *Sim) at(t time.Time, name string, fn func()) {
	s.seq++
	if t.Before(s.now()) {
		t = s.now()
	}
	heap.Push(&s.events, &event{at: t, seq: s.seq, name: name, fn: fn})
}

func (s *Sim) after(d time.Duration, name string, fn func()) { s.at(s.now().Add(d), name, fn) }

func (s *Sim) atDeferred(t time.Time, name string, fn func()) {
	s.seq++
	if t.Before(s.now()) {
		t = s.now()
	}
	heap.Push(&s.events, &event{at: t, seq: s.seq, name: name, fn: fn, deferWhileDisrupting: true})
}

func (s *Sim) now() time.Time { return s.env.Clock.Now() }

// runUntil processes every event due at or before t, advancing the virtual clock to each, then sets the clock to t.
// While a disruption pass is in flight (runUntil re-entered from its validation wait), deferred events are held back
// and requeued when it returns.
func (s *Sim) runUntil(t time.Time) {
	var held []*event
	for s.events.Len() > 0 {
		next := s.events[0]
		if next.at.After(t) {
			break
		}
		heap.Pop(&s.events)
		if next.deferWhileDisrupting && s.disrupting {
			held = append(held, next)
			continue
		}
		if next.at.After(s.now()) {
			s.env.Clock.SetTime(next.at)
		}
		s.eventsRun++
		next.fn()
		s.flushHooks()
		if s.stopErr != nil {
			return
		}
	}
	if t.After(s.now()) {
		s.env.Clock.SetTime(t)
	}
	for _, e := range held {
		heap.Push(&s.events, e)
	}
}
