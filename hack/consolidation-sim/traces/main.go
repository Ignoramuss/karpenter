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

// traces derives the W6 trace-replay workload for hack/consolidation-sim from the Google cluster trace 2011
// (clusterdata-2011-2): task runs from task_events inside a window, scaled to absolute requests and subsampled, as
// a CSV of start_seconds,end_seconds,cpu_millicores,memory_mib plus a provenance file recording every input's
// SHA-256. README.md documents the mapping and the trace's license. From the repository root:
//
//	go run ./hack/consolidation-sim/traces --cache /tmp/clusterdata-2011-2
package main

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const (
	defaultBucket = "https://storage.googleapis.com/clusterdata-2011-2"
	checksumFile  = "SHA256SUM"
	shardCount    = 500
	// traceStart is the timestamp of the trace's first instant: timestamps count microseconds from 600 s before it
	// (ClusterData2011_2.md: "This corresponds to a trace timestamp of 600s").
	traceStart = 600 * microsPerSecond
	header     = "start_seconds,end_seconds,cpu_millicores,memory_mib"
)

type options struct {
	cache     string
	bucket    string
	offline   bool
	offset    time.Duration
	window    time.Duration
	sample    float64
	seed      int64
	cpuCores  float64
	memoryGiB float64
	out       string
}

func main() {
	var o options
	fs := flag.NewFlagSet("traces", flag.ExitOnError)
	fs.StringVar(&o.cache, "cache", "", "directory holding (or receiving) the bucket's SHA256SUM and task_events shards; required")
	fs.StringVar(&o.bucket, "bucket", defaultBucket, "trace bucket URL to download missing inputs from")
	fs.BoolVar(&o.offline, "offline", false, "never download; fail if an input is missing from --cache")
	fs.DurationVar(&o.offset, "offset", 6*time.Hour, "window start after the trace start (whole seconds)")
	fs.DurationVar(&o.window, "window", 3*time.Hour, "window length (whole seconds)")
	fs.Float64Var(&o.sample, "sample", 0.2, "fraction of tasks (job ID, task index) to keep, in (0, 1]")
	fs.Int64Var(&o.seed, "seed", 1, "seed of the task sample")
	fs.Float64Var(&o.cpuCores, "cpu-cores", 32, "cores of a normalized CPU request of 1.0 (the largest machine)")
	fs.Float64Var(&o.memoryGiB, "memory-gib", 128, "GiB of a normalized memory request of 1.0 (the largest machine)")
	fs.StringVar(&o.out, "out", "", "derived CSV to write (default hack/consolidation-sim/traces/google-2011-<window>.csv)")
	_ = fs.Parse(os.Args[1:])
	if err := run(o); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func (o *options) validate() error {
	switch {
	case o.cache == "":
		return errors.New("--cache is required")
	case o.offset < 0 || o.offset%time.Second != 0:
		return fmt.Errorf("--offset %s must be a non-negative whole number of seconds", o.offset)
	case o.window <= 0 || o.window%time.Second != 0:
		return fmt.Errorf("--window %s must be a positive whole number of seconds", o.window)
	case !(o.sample > 0 && o.sample <= 1):
		return fmt.Errorf("--sample %v must be in (0, 1]", o.sample)
	case !(o.cpuCores > 0) || !(o.memoryGiB > 0):
		return errors.New("--cpu-cores and --memory-gib must be positive")
	}
	if o.out == "" {
		o.out = filepath.Join("hack", "consolidation-sim", "traces", "google-2011-"+windowLabel(o.offset, o.window)+".csv")
	}
	return nil
}

// windowLabel names a window by its bounds after the trace start, "6h-9h", or in minutes when not whole hours.
func windowLabel(offset, window time.Duration) string {
	if offset%time.Hour == 0 && window%time.Hour == 0 {
		return fmt.Sprintf("%dh-%dh", offset/time.Hour, (offset+window)/time.Hour)
	}
	return fmt.Sprintf("%dm-%dm", offset/time.Minute, (offset+window)/time.Minute)
}

// Input is one source file and its SHA-256, verified against the bucket's SHA256SUM.
type Input struct {
	Name   string `json:"name"`
	SHA256 string `json:"sha256"`
}

// Provenance is written next to the derived CSV. It holds no timestamps or local paths, so re-running the tool
// with the same flags reproduces it byte for byte.
type Provenance struct {
	Source      string `json:"source"`
	License     string `json:"license"`
	Attribution string `json:"attribution"`
	Checksums   Input  `json:"checksums"`
	// Inputs are the task_events shards read, in order: every shard from the trace start through the window end,
	// since a run that started before the window needs its SCHEDULE event.
	Inputs []Input `json:"inputs"`
	Window struct {
		Offset        string `json:"offset"`
		Length        string `json:"length"`
		StartMicros   int64  `json:"start_trace_micros"`
		EndMicros     int64  `json:"end_trace_micros"`
		DescribedTime string `json:"described_time"`
	} `json:"window"`
	Scale struct {
		CPUCores         float64 `json:"cpu_cores_per_normalized_unit"`
		MemoryGiB        float64 `json:"memory_gib_per_normalized_unit"`
		MinCPUMillicores int64   `json:"min_cpu_millicores"`
		MinMemoryMiB     int64   `json:"min_memory_mib"`
	} `json:"scale"`
	Sample struct {
		Fraction float64 `json:"fraction"`
		Seed     int64   `json:"seed"`
		Method   string  `json:"method"`
	} `json:"sample"`
	Counts Counts `json:"counts"`
	Output struct {
		Name   string `json:"name"`
		SHA256 string `json:"sha256"`
		Bytes  int    `json:"bytes"`
	} `json:"output"`
}

func run(o options) error {
	if err := o.validate(); err != nil {
		return err
	}
	sums, sumsInput, err := checksums(o)
	if err != nil {
		return err
	}
	start := traceStart + o.offset.Microseconds()
	end := start + o.window.Microseconds()
	d := newDeriver(start, end, o.cpuCores, o.memoryGiB, sampler{seed: o.seed, fraction: o.sample})
	var inputs []Input
	for i := 0; i < shardCount && !d.done; i++ {
		name := fmt.Sprintf("task_events/part-%05d-of-%05d.csv.gz", i, shardCount)
		in, err := fetch(o, name, sums)
		if err != nil {
			return err
		}
		inputs = append(inputs, in)
		if err := readShard(filepath.Join(o.cache, filepath.FromSlash(name)), d); err != nil {
			return fmt.Errorf("%s, %w", name, err)
		}
	}
	if !d.done {
		return fmt.Errorf("the trace ends before the window end (offset %s + window %s)", o.offset, o.window)
	}
	rows := d.finish()
	var buf bytes.Buffer
	buf.WriteString(header + "\n")
	for _, r := range rows {
		fmt.Fprintf(&buf, "%d,%d,%d,%d\n", r.Start, r.End, r.CPUMillicores, r.MemoryMiB)
	}
	if err := os.WriteFile(o.out, buf.Bytes(), 0o644); err != nil {
		return err
	}
	p := provenance(o, sumsInput, inputs, d.counts)
	sum := sha256.Sum256(buf.Bytes())
	p.Output.Name, p.Output.SHA256, p.Output.Bytes = filepath.Base(o.out), hex.EncodeToString(sum[:]), buf.Len()
	raw, err := json.MarshalIndent(p, "", "  ")
	if err != nil {
		return err
	}
	provPath := strings.TrimSuffix(o.out, ".csv") + ".provenance.json"
	if err := os.WriteFile(provPath, append(raw, '\n'), 0o644); err != nil {
		return err
	}
	fmt.Printf("wrote %s (%d rows, %d bytes, sha256 %s) and %s from %d shards\n", o.out, len(rows), buf.Len(), p.Output.SHA256, provPath, len(inputs))
	return nil
}

func provenance(o options, sums Input, inputs []Input, c Counts) Provenance {
	var p Provenance
	p.Source = "Google cluster trace 2011 v2.1 (clusterdata-2011-2), " + o.bucket + "/"
	p.License = "CC-BY 4.0, https://creativecommons.org/licenses/by/4.0/ (google/cluster-data README.md and ClusterData2011_2.md)"
	p.Attribution = "Google cluster-usage traces (clusterdata-2011-2), John Wilkes and Charles Reiss, Google Inc.; " +
		"format: C. Reiss, J. Wilkes, J. L. Hellerstein, \"Google cluster-usage traces: format + schema\", 2011, revised 2014-11-17 for v2.1. " +
		"Derived here: one window of task_events, scaled to absolute requests and subsampled (see README.md)."
	p.Checksums = sums
	p.Inputs = inputs
	p.Window.Offset, p.Window.Length = o.offset.String(), o.window.String()
	p.Window.StartMicros = traceStart + o.offset.Microseconds()
	p.Window.EndMicros = p.Window.StartMicros + o.window.Microseconds()
	// The trace starts at 19:00 EDT on Sunday May 1, 2011 (ClusterData2011_2.md); the data center is in US Eastern.
	edt := time.FixedZone("EDT", -4*60*60)
	t0 := time.Date(2011, time.May, 1, 19, 0, 0, 0, edt)
	p.Window.DescribedTime = t0.Add(o.offset).Format("2006-01-02 15:04 MST") + " to " + t0.Add(o.offset+o.window).Format("2006-01-02 15:04 MST")
	p.Scale.CPUCores, p.Scale.MemoryGiB = o.cpuCores, o.memoryGiB
	p.Scale.MinCPUMillicores, p.Scale.MinMemoryMiB = minCPUMillicores, minMemoryMiB
	p.Sample.Fraction, p.Sample.Seed = o.sample, o.seed
	p.Sample.Method = "keep task (job ID, task index) when the first 8 bytes of SHA-256(\"<seed>/<job ID>/<task index>\"), big-endian, are below fraction * 2^64"
	p.Counts = c
	return p
}

// checksums loads the bucket's SHA256SUM, downloading it into the cache if needed. Lines are "<hex> *<object>".
func checksums(o options) (map[string]string, Input, error) {
	path := filepath.Join(o.cache, checksumFile)
	if err := ensure(o, checksumFile, path); err != nil {
		return nil, Input{}, err
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, Input{}, err
	}
	sums := map[string]string{}
	for i, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		f := strings.Fields(line)
		if len(f) != 2 {
			return nil, Input{}, fmt.Errorf("%s line %d: want \"<sha256> *<name>\", got %q", checksumFile, i+1, line)
		}
		sums[strings.TrimPrefix(f[1], "*")] = f[0]
	}
	h := sha256.Sum256(raw)
	return sums, Input{Name: checksumFile, SHA256: hex.EncodeToString(h[:])}, nil
}

// fetch makes sure the cache holds the object and that its SHA-256 matches SHA256SUM.
func fetch(o options, name string, sums map[string]string) (Input, error) {
	want, ok := sums[name]
	if !ok {
		return Input{}, fmt.Errorf("%s is not listed in %s", name, checksumFile)
	}
	path := filepath.Join(o.cache, filepath.FromSlash(name))
	if err := ensure(o, name, path); err != nil {
		return Input{}, err
	}
	got, err := fileSHA256(path)
	if err != nil {
		return Input{}, err
	}
	if got != want {
		return Input{}, fmt.Errorf("%s: sha256 %s, but %s lists %s; delete it from the cache to download it again", path, got, checksumFile, want)
	}
	return Input{Name: name, SHA256: got}, nil
}

func ensure(o options, name, path string) error {
	if _, err := os.Stat(path); err == nil {
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if o.offline {
		return fmt.Errorf("%s is missing and --offline is set", path)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return download(o.bucket+"/"+name, path)
}

var httpClient = &http.Client{Timeout: 10 * time.Minute}

// download writes url to path through a temporary file, so an interrupted transfer never looks like a cached input.
func download(url, path string) error {
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	// The shards are stored gzip-compressed with identity encoding; ask for the stored bytes so the checksum applies.
	req.Header.Set("Accept-Encoding", "identity")
	resp, err := httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("downloading %s, %w", url, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("downloading %s: %s", url, resp.Status)
	}
	tmp := path + ".part"
	f, err := os.Create(tmp)
	if err != nil {
		return err
	}
	if _, err := io.Copy(f, resp.Body); err != nil {
		f.Close()
		return fmt.Errorf("downloading %s, %w", url, err)
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func fileSHA256(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// readShard streams one gzip-compressed task_events shard into the deriver until it reports the window done.
func readShard(path string, d *deriver) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	gz, err := gzip.NewReader(bufio.NewReader(f))
	if err != nil {
		return err
	}
	defer gz.Close()
	r := csv.NewReader(gz)
	r.FieldsPerRecord = eventColumns
	r.ReuseRecord = true
	for line := 1; !d.done; line++ {
		rec, err := r.Read()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
		if err := d.event(rec); err != nil {
			return fmt.Errorf("line %d: %w", line, err)
		}
	}
	return nil
}
