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

package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"maps"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strconv"
	"strings"

	"sigs.k8s.io/yaml"

	"sigs.k8s.io/karpenter/hack/consolidation-sim/sim"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
)

// DefaultMatrixOutDir is where cells land, relative to the working directory, when neither the matrix file nor
// --out names a directory.
const DefaultMatrixOutDir = "experiments/runs"

const (
	// maxCells bounds an expansion so a typo in a range fails at once instead of exhausting memory.
	maxCells = 100_000
	// maxGroupLen and maxDescLen keep a run ID well inside a file name's 255 bytes; the hash keeps it unique.
	maxGroupLen = 64
	maxDescLen  = 96
)

// Matrix is an experiment matrix file. A cell is base, then its group's set, then one combination of the group's
// vary values:
//
//	name: session
//	outDir: experiments/runs
//	base: {scenario: {name: W1}, duration: 3h}
//	groups:
//	- name: exp1
//	  set: {placement: least-allocated}
//	  vary:
//	    arm: [B0-none, B2-when-empty-or-underutilized]
//	    scenario.nodes: [50, 200]
//	    seed: {from: 101, to: 110}
//
// Wherever sim.Config holds a struct with a name field (arm, scenario, a pipeline stage, an entry of a pipeline's
// moves), a string stands for {name: <string>}, so "arm: B0-none" works in base, set and vary alike.
type Matrix struct {
	// Name labels progress output; it defaults to the file's base name.
	Name string `json:"name"`
	// OutDir holds one directory per cell plus index.jsonl, relative to the working directory.
	OutDir string `json:"outDir,omitempty"`
	// Definitions is ignored; it holds YAML anchors that groups reference (for example shared pipeline stages).
	Definitions json.RawMessage `json:"definitions,omitempty"`
	// Base is a partial sim.Config shared by every group.
	Base   json.RawMessage `json:"base,omitempty"`
	Groups []MatrixGroup   `json:"groups"`
}

// MatrixGroup is one cartesian product of settings.
type MatrixGroup struct {
	// Name prefixes the group's run IDs, so renaming a group reruns its cells.
	Name string `json:"name"`
	// Set is a partial sim.Config merged over base as a JSON merge patch (RFC 7386): objects merge key by key, null
	// deletes, anything else replaces.
	Set json.RawMessage `json:"set,omitempty"`
	// Vary maps a dotted sim.Config field path to its values: a list, or an inclusive integer range {from, to}. Each
	// value replaces whatever is at the path, except that a string at a struct with a name field sets only the name
	// and keeps the struct's other fields.
	Vary map[string]json.RawMessage `json:"vary,omitempty"`
}

// Cell is one expanded run.
type Cell struct {
	Group string
	RunID string
	// Config is the cell's sim.Config as a generic object without runID. Its canonical JSON is what the run ID
	// hashes, so the ID depends on what the matrix says rather than on sim.Config's Go fields: adding a field to
	// sim.Config does not rename, and so rerun, every finished cell.
	Config map[string]any
}

// loadMatrix reads a matrix file, rejecting unknown and duplicate keys.
func loadMatrix(path string) (*Matrix, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	js, err := yaml.YAMLToJSONStrict(raw)
	if err != nil {
		return nil, fmt.Errorf("parsing %s, %w", path, err)
	}
	m := &Matrix{}
	if err := decodeStrict(js, m); err != nil {
		return nil, fmt.Errorf("parsing %s, %w", path, err)
	}
	if m.Name == "" {
		m.Name = strings.TrimSuffix(filepath.Base(path), filepath.Ext(path))
	}
	if m.OutDir == "" {
		m.OutDir = DefaultMatrixOutDir
	}
	return m, nil
}

// decodeStrict keeps numbers as json.Number so int64 seeds survive the generic maps exactly.
func decodeStrict(raw []byte, out any) error {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	dec.UseNumber()
	if err := dec.Decode(out); err != nil {
		return err
	}
	if dec.More() {
		return fmt.Errorf("unexpected data after the top-level value")
	}
	return nil
}

// Expand returns the cells of the named groups (every group when only is empty), groups in file order and each
// group's cells in odometer order over its vary paths sorted lexically, the last path varying fastest. Every cell is
// decoded strictly into sim.Config and validated here, so a malformed matrix fails before anything runs.
func (m *Matrix) Expand(only []string) ([]Cell, error) {
	selected, err := m.selectGroups(only)
	if err != nil {
		return nil, err
	}
	base, err := decodeObject(m.Base, "base")
	if err != nil {
		return nil, err
	}
	var cells []Cell
	owner := map[string]string{}
	for _, g := range m.Groups {
		if !selected[g.Name] {
			continue
		}
		gc, err := g.expand(base, maxCells-len(cells))
		if err != nil {
			return nil, fmt.Errorf("group %s, %w", g.Name, err)
		}
		for _, c := range gc {
			if other, dup := owner[c.RunID]; dup {
				return nil, fmt.Errorf("groups %s and %s both expand to run ID %s", other, g.Name, c.RunID)
			}
			owner[c.RunID] = g.Name
		}
		cells = append(cells, gc...)
	}
	return cells, nil
}

func (m *Matrix) selectGroups(only []string) (map[string]bool, error) {
	if len(m.Groups) == 0 {
		return nil, fmt.Errorf("matrix %s has no groups", m.Name)
	}
	names := map[string]bool{}
	prefixes := map[string]string{}
	for i, g := range m.Groups {
		if g.Name == "" {
			return nil, fmt.Errorf("group %d has no name", i+1)
		}
		if names[g.Name] {
			return nil, fmt.Errorf("duplicate group %s", g.Name)
		}
		p := groupPrefix(g.Name)
		if p == "" {
			return nil, fmt.Errorf("group %q has no characters usable in a run ID", g.Name)
		}
		if other, ok := prefixes[p]; ok {
			return nil, fmt.Errorf("groups %s and %s both give run ID prefix %s", other, g.Name, p)
		}
		names[g.Name], prefixes[p] = true, g.Name
	}
	if len(only) == 0 {
		return names, nil
	}
	selected := map[string]bool{}
	for _, n := range only {
		if !names[n] {
			return nil, fmt.Errorf("--only names unknown group %q", n)
		}
		selected[n] = true
	}
	return selected, nil
}

// setting is one vary value, resolved to where it lands.
type setting struct {
	segs  []string
	value any
	// token is the value's part of the run ID's readable description.
	token string
}

type axis struct {
	path     string
	settings []setting
}

func (g MatrixGroup) expand(base map[string]any, limit int) ([]Cell, error) {
	set, err := decodeObject(g.Set, "set")
	if err != nil {
		return nil, err
	}
	merged := mergePatch(deepCopy(base).(map[string]any), set)
	axes, err := g.axes()
	if err != nil {
		return nil, err
	}
	n := 1
	for _, a := range axes {
		if n *= len(a.settings); n > limit {
			return nil, fmt.Errorf("matrix expands to more than %d cells", maxCells)
		}
	}
	cells := make([]Cell, 0, n)
	seen := map[string]string{}
	idx := make([]int, len(axes))
	for range n {
		cfg := deepCopy(merged).(map[string]any)
		tokens := make([]string, 0, len(axes))
		for i, a := range axes {
			s := a.settings[idx[i]]
			if err := setPath(cfg, s.segs, s.value); err != nil {
				return nil, fmt.Errorf("vary %s, %w", a.path, err)
			}
			tokens = append(tokens, s.token)
		}
		desc := strings.Join(tokens, "_")
		if desc == "" {
			desc = "base"
		}
		if err := validateCell(cfg); err != nil {
			return nil, fmt.Errorf("cell %s, %w", desc, err)
		}
		hash := configHash(cfg)
		if other, dup := seen[hash]; dup {
			return nil, fmt.Errorf("cells %s and %s have the same config", other, desc)
		}
		seen[hash] = desc
		cells = append(cells, Cell{Group: g.Name, RunID: runID(g.Name, desc, hash), Config: cfg})
		for i := len(axes) - 1; i >= 0; i-- {
			if idx[i]++; idx[i] < len(axes[i].settings) {
				break
			}
			idx[i] = 0
		}
	}
	return cells, nil
}

func (g MatrixGroup) axes() ([]axis, error) {
	paths := slices.Sorted(maps.Keys(g.Vary))
	axes := make([]axis, 0, len(paths))
	for _, p := range paths {
		segs := strings.Split(p, ".")
		if slices.Contains(segs, "") {
			return nil, fmt.Errorf("vary path %q has an empty segment", p)
		}
		if segs[0] == "runID" {
			return nil, fmt.Errorf("vary sets runID; the runner derives it")
		}
		t, err := fieldPathType(segs)
		if err != nil {
			return nil, fmt.Errorf("vary %s, %w", p, err)
		}
		values, err := varyValues(g.Vary[p])
		if err != nil {
			return nil, fmt.Errorf("vary %s, %w", p, err)
		}
		a := axis{path: p}
		for _, v := range values {
			s := setting{segs: segs, value: normalize(t, v), token: describe(p, v)}
			if str, ok := v.(string); ok && hasNameField(t) {
				// Setting only the name keeps the rest of the struct, e.g. arm.policy from base.
				s.segs, s.value = append(slices.Clone(segs), "name"), str
			}
			a.settings = append(a.settings, s)
		}
		axes = append(axes, a)
	}
	return axes, nil
}

// varyValues reads a vary entry: a non-empty list, or an inclusive integer range {from, to}.
func varyValues(raw json.RawMessage) ([]any, error) {
	var v any
	if err := decodeStrict(raw, &v); err != nil {
		return nil, err
	}
	switch x := v.(type) {
	case []any:
		if len(x) == 0 {
			return nil, fmt.Errorf("no values")
		}
		return x, nil
	case map[string]any:
		return intRange(raw)
	default:
		return nil, fmt.Errorf("want a list of values or {from, to}, got %s", raw)
	}
}

func intRange(raw json.RawMessage) ([]any, error) {
	var r struct {
		From *json.Number `json:"from"`
		To   *json.Number `json:"to"`
	}
	if err := decodeStrict(raw, &r); err != nil {
		return nil, fmt.Errorf("range, %w", err)
	}
	if r.From == nil || r.To == nil {
		return nil, fmt.Errorf("range needs both from and to")
	}
	from, err := r.From.Int64()
	if err != nil {
		return nil, fmt.Errorf("range from %s is not an integer", r.From)
	}
	to, err := r.To.Int64()
	if err != nil {
		return nil, fmt.Errorf("range to %s is not an integer", r.To)
	}
	// A negative span is either to < from or an int64 overflow; both are mistakes.
	span := to - from
	if span < 0 || span >= maxCells {
		return nil, fmt.Errorf("range {from: %d, to: %d} must have from <= to and fewer than %d values", from, to, maxCells)
	}
	values := make([]any, 0, span+1)
	for i := int64(0); i <= span; i++ {
		values = append(values, json.Number(strconv.FormatInt(from+i, 10)))
	}
	return values, nil
}

// decodeObject reads base or set, normalized so that the shorthand and the long form hash alike.
func decodeObject(raw json.RawMessage, what string) (map[string]any, error) {
	if len(bytes.TrimSpace(raw)) == 0 || bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
		return map[string]any{}, nil
	}
	var v any
	if err := decodeStrict(raw, &v); err != nil {
		return nil, fmt.Errorf("%s, %w", what, err)
	}
	m, ok := v.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("%s must be an object, got %s", what, raw)
	}
	if _, ok := m["runID"]; ok {
		return nil, fmt.Errorf("%s sets runID; the runner derives it", what)
	}
	return normalize(reflect.TypeFor[sim.Config](), m).(map[string]any), nil
}

// mergePatch applies patch to dst as a JSON merge patch (RFC 7386) and returns dst.
func mergePatch(dst, patch map[string]any) map[string]any {
	for k, v := range patch {
		switch pv := v.(type) {
		case nil:
			delete(dst, k)
		case map[string]any:
			dv, ok := dst[k].(map[string]any)
			if !ok {
				dv = map[string]any{}
			}
			dst[k] = mergePatch(dv, pv)
		default:
			dst[k] = deepCopy(v)
		}
	}
	return dst
}

// setPath sets a dotted path in a generic config, creating intermediate objects; a null value deletes the key.
func setPath(cfg map[string]any, segs []string, v any) error {
	m := cfg
	for i, s := range segs[:len(segs)-1] {
		switch next := m[s].(type) {
		case nil:
			child := map[string]any{}
			m[s], m = child, child
		case map[string]any:
			m = next
		default:
			return fmt.Errorf("%s is %v, not an object", strings.Join(segs[:i+1], "."), next)
		}
	}
	last := segs[len(segs)-1]
	if v == nil {
		delete(m, last)
		return nil
	}
	m[last] = deepCopy(v)
	return nil
}

func deepCopy(v any) any {
	switch x := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, e := range x {
			out[k] = deepCopy(e)
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = deepCopy(e)
		}
		return out
	default:
		return v
	}
}

// validateCell decodes a cell the way the run command will and checks what can be checked without running it, the
// pipeline's strategy names included: this binary registers the same strategies as the subprocess.
func validateCell(cfg map[string]any) error {
	raw, err := json.Marshal(cfg)
	if err != nil {
		return err
	}
	var c sim.Config
	if err := decodeStrict(raw, &c); err != nil {
		return err
	}
	c.Defaults()
	if err := c.Validate(); err != nil {
		return err
	}
	if c.Arm.Pipeline != nil {
		praw, err := json.Marshal(c.Arm.Pipeline)
		if err != nil {
			return err
		}
		if _, err := pipeline.ParseConfig(praw); err != nil {
			return fmt.Errorf("arm.pipeline, %w", err)
		}
	}
	return nil
}

// configHash is 8 hex digits of the SHA-256 of the canonical JSON: encoding/json sorts map keys, and json.Number
// keeps each number's literal.
func configHash(cfg map[string]any) string {
	return shortHash(cfg, 4)
}

func shortHash(v any, n int) string {
	raw, err := json.Marshal(v)
	if err != nil {
		// Only decoded JSON reaches here, and it always encodes.
		panic(err)
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:n])
}

// runID is "<group>__<description>__<hash>". Only the hash identifies the config; the rest is for people reading
// directory listings, so truncating it is harmless.
func runID(group, desc, hash string) string {
	if len(desc) > maxDescLen {
		desc = strings.TrimRight(desc[:maxDescLen], "_-.")
	}
	return groupPrefix(group) + "__" + desc + "__" + hash
}

func groupPrefix(group string) string {
	p := sanitize(group)
	if len(p) > maxGroupLen {
		p = strings.TrimRight(p[:maxGroupLen], "-.")
	}
	return p
}

// describe renders one vary value as "<key>-<value>", the key being the path's last segment other than "name", so
// "arm.pipeline.planner.name" reads as "planner".
func describe(path string, v any) string {
	segs := strings.Split(path, ".")
	for len(segs) > 1 && segs[len(segs)-1] == "name" {
		segs = segs[:len(segs)-1]
	}
	return sanitize(segs[len(segs)-1]) + "-" + describeValue(v)
}

func describeValue(v any) string {
	var s string
	switch x := v.(type) {
	case string:
		s = sanitize(x)
	case json.Number:
		s = sanitize(x.String())
	case bool:
		s = strconv.FormatBool(x)
	case nil:
		s = "null"
	case map[string]any:
		// A label (arms carry one) says more than a name.
		if label, ok := x["label"].(string); ok {
			s = sanitize(label)
		} else if name, ok := x["name"].(string); ok {
			s = sanitize(name)
		}
	}
	if s == "" {
		s = "v" + shortHash(v, 3)
	}
	return s
}

// sanitize maps s onto the run ID alphabet less '_', which separates a run ID's parts and its description's tokens.
func sanitize(s string) string {
	var b strings.Builder
	dash := false
	for _, r := range strings.ToLower(s) {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '.' {
			b.WriteRune(r)
			dash = false
		} else if !dash {
			b.WriteByte('-')
			dash = true
		}
	}
	return strings.Trim(b.String(), "-.")
}

var unmarshalerType = reflect.TypeFor[json.Unmarshaler]()

func deref(t reflect.Type) reflect.Type {
	for t != nil && t.Kind() == reflect.Pointer {
		t = t.Elem()
	}
	return t
}

// childType is the type of key inside t, or nil where t is not an object sim.Config's decoder descends into: a
// free-form value, a scalar, a list, or a type that decodes itself (Duration, StageList).
func childType(t reflect.Type, key string) reflect.Type {
	t = deref(t)
	switch {
	case t == nil:
		return nil
	case t.Kind() == reflect.Map && t.Key().Kind() == reflect.String:
		return t.Elem()
	case t.Kind() == reflect.Struct && !reflect.PointerTo(t).Implements(unmarshalerType):
		if f, ok := jsonField(t, key); ok {
			return f.Type
		}
	}
	return nil
}

// jsonField finds a field by its exact JSON name. encoding/json would also accept other cases, but a vary path that
// differs from base only in case would then put two keys for one field into the same object.
func jsonField(t reflect.Type, name string) (reflect.StructField, bool) {
	for i := range t.NumField() {
		f := t.Field(i)
		tag := f.Tag.Get("json")
		if !f.IsExported() || tag == "-" {
			continue
		}
		n, _, _ := strings.Cut(tag, ",")
		if n == "" {
			n = f.Name
		}
		if n == name {
			return f, true
		}
	}
	return reflect.StructField{}, false
}

// fieldPathType resolves a vary path against sim.Config so a typo fails at expansion rather than as two failed
// attempts of every cell. Below a free-form value (scenario.params, a stage's params) anything goes, and it returns
// nil.
func fieldPathType(segs []string) (reflect.Type, error) {
	t := reflect.TypeFor[sim.Config]()
	for i, s := range segs {
		if t = deref(t); t.Kind() == reflect.Interface {
			return nil, nil
		}
		if t = childType(t, s); t == nil {
			return nil, fmt.Errorf("sim.Config has no field %s", strings.Join(segs[:i+1], "."))
		}
	}
	return t, nil
}

// hasNameField says whether a string stands for {name: <string>} at a value of type t.
func hasNameField(t reflect.Type) bool {
	t = deref(t)
	if t == nil || t.Kind() != reflect.Struct || reflect.PointerTo(t).Implements(unmarshalerType) {
		return false
	}
	f, ok := jsonField(t, "name")
	return ok && f.Type.Kind() == reflect.String
}

// normalize expands the name shorthand throughout a value of type t, in place where it can. Lists are included, so
// a pipeline's moves can be written [upstream-multi, upstream-single].
func normalize(t reflect.Type, v any) any {
	switch x := v.(type) {
	case string:
		if hasNameField(t) {
			return map[string]any{"name": x}
		}
	case map[string]any:
		for k, e := range x {
			x[k] = normalize(childType(t, k), e)
		}
	case []any:
		if t = deref(t); t != nil && (t.Kind() == reflect.Slice || t.Kind() == reflect.Array) {
			for i, e := range x {
				x[i] = normalize(t.Elem(), e)
			}
		}
	}
	return v
}
