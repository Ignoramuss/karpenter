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
	"encoding/json"
	"io"
	"sync"
)

// NopRecorder discards decision events.
type NopRecorder struct{}

func (NopRecorder) Record(DecisionEvent) {}

// JSONLRecorder writes one JSON object per line. encoding/json sorts map keys and formats floats canonically, so
// identical event sequences produce identical bytes, which the determinism test relies on.
type JSONLRecorder struct {
	mu  sync.Mutex
	enc *json.Encoder
	err error
}

func NewJSONLRecorder(w io.Writer) *JSONLRecorder { return &JSONLRecorder{enc: json.NewEncoder(w)} }

func (r *JSONLRecorder) Record(e DecisionEvent) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.err == nil {
		r.err = r.enc.Encode(e)
	}
}

// Err returns the first write error.
func (r *JSONLRecorder) Err() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.err
}

// MemoryRecorder keeps events in memory, for tests.
type MemoryRecorder struct {
	mu     sync.Mutex
	Events []DecisionEvent
}

func (r *MemoryRecorder) Record(e DecisionEvent) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.Events = append(r.Events, e)
}

// MultiRecorder fans events out.
type MultiRecorder []Recorder

func (m MultiRecorder) Record(e DecisionEvent) {
	for _, r := range m {
		r.Record(e)
	}
}
