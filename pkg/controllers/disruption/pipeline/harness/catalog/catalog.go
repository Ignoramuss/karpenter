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

// Package catalog loads the instance types the simulator offers from the KWOK example catalog shipped in the repo
// (kwok/examples/aws_instance_types.json). It keeps the families, operating system and capacity type a run asks for
// and builds cloudprovider.InstanceTypes shaped like the KWOK provider's, so the real scheduler sees what it would see
// on a KWOK cluster.
package catalog

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/samber/lo"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"

	v1 "sigs.k8s.io/karpenter/pkg/apis/v1"
	"sigs.k8s.io/karpenter/pkg/cloudprovider"
	"sigs.k8s.io/karpenter/pkg/scheduling"
)

// DefaultPath is the catalog the plan fixes for every run, relative to the repository root.
const DefaultPath = "kwok/examples/aws_instance_types.json"

// The KWOK provider's instance labels (kwok/apis/v1alpha1/labels.go), repeated so this package does not import kwok.
const (
	InstanceSizeLabelKey   = "karpenter.kwok.sh/instance-size"
	InstanceFamilyLabelKey = "karpenter.kwok.sh/instance-family"
	InstanceMemoryLabelKey = "karpenter.kwok.sh/instance-memory"
	InstanceCPULabelKey    = "karpenter.kwok.sh/instance-cpu"
)

// The KWOK provider registers its instance labels as well-known and its domain as restricted
// (kwok/apis/v1alpha1/labels.go); instance types built here carry those labels, so the scheduler must see the same
// registration or it rejects them as undefined requirements.
func init() {
	v1.RestrictedLabelDomains = v1.RestrictedLabelDomains.Insert("karpenter.kwok.sh")
	v1.WellKnownLabels = v1.WellKnownLabels.Insert(InstanceSizeLabelKey, InstanceFamilyLabelKey, InstanceCPULabelKey, InstanceMemoryLabelKey)
}

// Families returns the families present in the catalog, sorted.
func (c *Catalog) Families() []string {
	fams := lo.Uniq(lo.Map(c.Entries, func(e Entry, _ int) string { return e.Family }))
	sort.Strings(fams)
	return fams
}

// DefaultFamilies are the on-demand families the Balanced RFC's analysis uses.
var DefaultFamilies = []string{"c7i", "m7i", "r7i"}

// Options selects a subset of the catalog.
type Options struct {
	Path          string   `json:"path" yaml:"path"`
	Families      []string `json:"families" yaml:"families"`
	CapacityTypes []string `json:"capacityTypes" yaml:"capacityTypes"`
	Zones         []string `json:"zones" yaml:"zones"`
	// MaxCPU drops instance types larger than this many vCPUs (0 keeps all). Large metal sizes add scheduler work
	// without changing which types consolidation picks for the scenarios here.
	MaxCPU int64 `json:"maxCPU" yaml:"maxCPU"`
}

func (o Options) withDefaults() Options {
	if o.Path == "" {
		o.Path = RepoPath(DefaultPath)
	}
	if len(o.Families) == 0 {
		o.Families = DefaultFamilies
	}
	if len(o.CapacityTypes) == 0 {
		o.CapacityTypes = []string{v1.CapacityTypeOnDemand}
	}
	return o
}

type rawRequirement struct {
	Key      string   `json:"key"`
	Operator string   `json:"operator"`
	Values   []string `json:"values"`
}

type rawOffering struct {
	Price        float64          `json:"Price"`
	Available    bool             `json:"Available"`
	Requirements []rawRequirement `json:"Requirements"`
}

type rawInstanceType struct {
	Name             string            `json:"name"`
	Offerings        []rawOffering     `json:"offerings"`
	Architecture     string            `json:"architecture"`
	OperatingSystems []string          `json:"operatingSystems"`
	Resources        map[string]string `json:"resources"`
}

// Entry is one instance type in a loaded catalog, with the scalar fields the simulator's own models need.
type Entry struct {
	InstanceType *cloudprovider.InstanceType
	Family       string
	Size         string
	CPU          int64 // millicores of capacity
	Memory       int64 // bytes of capacity
	Pods         int64
	// Price is the cheapest on-demand price across zones, $/hr.
	Price float64
}

// Catalog is an ordered list of instance types, sorted by name for deterministic iteration.
type Catalog struct {
	Entries []Entry
	Zones   []string
	byName  map[string]*Entry
}

// Load reads the catalog file and keeps linux instance types of the requested families, with only the requested
// capacity types' offerings.
func Load(opts Options) (*Catalog, error) {
	opts = opts.withDefaults()
	raw, err := os.ReadFile(opts.Path)
	if err != nil {
		return nil, fmt.Errorf("reading catalog %s, %w", opts.Path, err)
	}
	var items []rawInstanceType
	if err := json.Unmarshal(raw, &items); err != nil {
		return nil, fmt.Errorf("parsing catalog %s, %w", opts.Path, err)
	}
	families := lo.SliceToMap(opts.Families, func(f string) (string, bool) { return f, true })
	capacityTypes := lo.SliceToMap(opts.CapacityTypes, func(c string) (string, bool) { return c, true })
	zoneFilter := lo.SliceToMap(opts.Zones, func(z string) (string, bool) { return z, true })
	c := &Catalog{byName: map[string]*Entry{}}
	zones := map[string]bool{}
	for _, it := range items {
		family, size, _ := strings.Cut(it.Name, ".")
		if !families[family] || !lo.Contains(it.OperatingSystems, string(corev1.Linux)) {
			continue
		}
		if _, dup := c.byName[it.Name]; dup {
			continue
		}
		cpu := resource.MustParse(it.Resources["cpu"])
		if opts.MaxCPU > 0 && cpu.Value() > opts.MaxCPU {
			continue
		}
		var offerings []rawOffering
		for _, o := range it.Offerings {
			ct, zone := requirementValue(o.Requirements, v1.CapacityTypeLabelKey), requirementValue(o.Requirements, corev1.LabelTopologyZone)
			if !capacityTypes[ct] || (len(zoneFilter) > 0 && !zoneFilter[zone]) {
				continue
			}
			offerings = append(offerings, o)
			zones[zone] = true
		}
		if len(offerings) == 0 {
			continue
		}
		entry := newEntry(it, family, size, offerings)
		c.Entries = append(c.Entries, entry)
		c.byName[it.Name] = &c.Entries[len(c.Entries)-1]
	}
	sort.Slice(c.Entries, func(i, j int) bool { return c.Entries[i].InstanceType.Name < c.Entries[j].InstanceType.Name })
	c.byName = map[string]*Entry{}
	for i := range c.Entries {
		c.byName[c.Entries[i].InstanceType.Name] = &c.Entries[i]
	}
	c.Zones = lo.Keys(zones)
	sort.Strings(c.Zones)
	if len(c.Entries) == 0 {
		return nil, fmt.Errorf("catalog %s has no instance types for families %v", opts.Path, opts.Families)
	}
	return c, nil
}

// InstanceTypes returns the cloudprovider view of every entry, in name order.
func (c *Catalog) InstanceTypes() []*cloudprovider.InstanceType {
	return lo.Map(c.Entries, func(e Entry, _ int) *cloudprovider.InstanceType { return e.InstanceType })
}

// Get returns the entry for an instance type name.
func (c *Catalog) Get(name string) (*Entry, bool) {
	e, ok := c.byName[name]
	return e, ok
}

// RepoPath resolves a repository-relative path by walking up from the working directory to the directory holding
// go.mod, so tests and tools find the catalog from any package directory.
func RepoPath(rel string) string {
	dir, err := os.Getwd()
	if err != nil {
		return rel
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return filepath.Join(dir, rel)
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return rel
		}
		dir = parent
	}
}

func requirementValue(reqs []rawRequirement, key string) string {
	for _, r := range reqs {
		if r.Key == key && len(r.Values) > 0 {
			return r.Values[0]
		}
	}
	return ""
}

// newEntry mirrors kwok/cloudprovider/helpers.go newInstanceType: the same requirement keys, the same 110 default pod
// limit when unset, and the same kube-reserved overhead, so the scheduler computes the same allocatable.
func newEntry(it rawInstanceType, family, size string, offerings []rawOffering) Entry {
	res := corev1.ResourceList{corev1.ResourcePods: resource.MustParse("110")}
	for k, v := range it.Resources {
		res[corev1.ResourceName(k)] = resource.MustParse(v)
	}
	zones := lo.Uniq(lo.Map(offerings, func(o rawOffering, _ int) string {
		return requirementValue(o.Requirements, corev1.LabelTopologyZone)
	}))
	sort.Strings(zones)
	capacityTypes := lo.Uniq(lo.Map(offerings, func(o rawOffering, _ int) string {
		return requirementValue(o.Requirements, v1.CapacityTypeLabelKey)
	}))
	sort.Strings(capacityTypes)
	cpu, mem := res[corev1.ResourceCPU], res[corev1.ResourceMemory]
	for _, o := range offerings {
		for _, r := range o.Requirements {
			v1.WellKnownLabels = v1.WellKnownLabels.Insert(r.Key)
		}
	}
	reqs := scheduling.NewRequirements(
		scheduling.NewRequirement(corev1.LabelInstanceTypeStable, corev1.NodeSelectorOpIn, it.Name),
		scheduling.NewRequirement(corev1.LabelArchStable, corev1.NodeSelectorOpIn, archLabel(it.Architecture)),
		scheduling.NewRequirement(corev1.LabelOSStable, corev1.NodeSelectorOpIn, string(corev1.Linux)),
		scheduling.NewRequirement(corev1.LabelTopologyZone, corev1.NodeSelectorOpIn, zones...),
		scheduling.NewRequirement(v1.CapacityTypeLabelKey, corev1.NodeSelectorOpIn, capacityTypes...),
		scheduling.NewRequirement(InstanceSizeLabelKey, corev1.NodeSelectorOpIn, size),
		scheduling.NewRequirement(InstanceFamilyLabelKey, corev1.NodeSelectorOpIn, family),
		scheduling.NewRequirement(InstanceCPULabelKey, corev1.NodeSelectorOpIn, cpu.String()),
		scheduling.NewRequirement(InstanceMemoryLabelKey, corev1.NodeSelectorOpIn, mem.String()),
	)
	sorted := append([]rawOffering(nil), offerings...)
	sort.SliceStable(sorted, func(i, j int) bool {
		zi, zj := requirementValue(sorted[i].Requirements, corev1.LabelTopologyZone), requirementValue(sorted[j].Requirements, corev1.LabelTopologyZone)
		if zi != zj {
			return zi < zj
		}
		return requirementValue(sorted[i].Requirements, v1.CapacityTypeLabelKey) < requirementValue(sorted[j].Requirements, v1.CapacityTypeLabelKey)
	})
	cpOfferings := lo.Map(sorted, func(o rawOffering, _ int) *cloudprovider.Offering {
		return &cloudprovider.Offering{
			Requirements: scheduling.NewRequirements(lo.Map(o.Requirements, func(r rawRequirement, _ int) *scheduling.Requirement {
				return scheduling.NewRequirement(r.Key, corev1.NodeSelectorOperator(r.Operator), r.Values...)
			})...),
			Price:     o.Price,
			Available: true,
		}
	})
	instanceType := &cloudprovider.InstanceType{
		Name:         it.Name,
		Requirements: reqs,
		Offerings:    cpOfferings,
		Capacity:     res,
		Overhead: &cloudprovider.InstanceTypeOverhead{
			KubeReserved: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("100m"),
				corev1.ResourceMemory: resource.MustParse("10Mi"),
			},
		},
	}
	pods := res[corev1.ResourcePods]
	return Entry{
		InstanceType: instanceType,
		Family:       family,
		Size:         size,
		CPU:          cpu.MilliValue(),
		Memory:       mem.Value(),
		Pods:         pods.Value(),
		Price:        lo.MinBy(cpOfferings, func(a, b *cloudprovider.Offering) bool { return a.Price < b.Price }).Price,
	}
}

func archLabel(a string) string {
	switch a {
	case "x86_64":
		return "amd64"
	case "arm64", "aarch64":
		return "arm64"
	}
	return a
}
