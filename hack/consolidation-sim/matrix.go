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
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"maps"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	// maxAttempts is how many failed attempts a cell gets before the runner abandons it.
	maxAttempts = 2
	// failedInfix names a failed attempt's directory, <runID>.failed-<n>. Run IDs end in hex, so it cannot be part
	// of one.
	failedInfix = ".failed-"
	// lockPoll is how often a runner waiting on another runner's lock checks again.
	lockPoll = time.Second
)

// Statuses in index.jsonl. A cell's last line is its current state: failed cells are retried by the next invocation,
// abandoned ones never.
const (
	statusDone      = "done"
	statusFailed    = "failed"
	statusAbandoned = "abandoned"
)

// indexEntry is one line of <outDir>/index.jsonl, appended as each attempt finishes. The directory tree, not the
// index, decides what is done: a cell whose runner died still counts once its summary.json lands.
type indexEntry struct {
	RunID       string    `json:"run_id"`
	Group       string    `json:"group"`
	Status      string    `json:"status"`
	ExitCode    int       `json:"exit_code"`
	WallSeconds float64   `json:"wall_seconds"`
	FinishedAt  time.Time `json:"finished_at"`
	Attempt     int       `json:"attempt"`
	Error       string    `json:"error,omitempty"`
}

// matrixCounts partitions a matrix's cells. Failed counts abandoned cells only; a cell with a retry left is pending.
type matrixCounts struct {
	Total, Done, Failed, Pending int
}

// runMatrix is the matrix command:
//
//	consolidation-sim matrix --matrix <file> [--workers N] [--max-wall 20m] [--out <dir>] [--only <group,...>]
//	    [--cell-max-wall <dur>] [--dry-run]
//
// It runs the matrix's pending cells, up to --workers at a time, until none are left or --max-wall has passed, then
// exits 0 so a caller can re-invoke it until it reports nothing pending. It exits 1 only when the matrix or the
// invocation is malformed or the output directory is unusable; a failing cell is recorded, not fatal.
func runMatrix(args []string) error { return matrixMain(args, os.Stdout) }

func matrixMain(args []string, stdout io.Writer) error {
	fset := flag.NewFlagSet("matrix", flag.ContinueOnError)
	path := fset.String("matrix", "", "matrix file (YAML or JSON)")
	workers := fset.Int("workers", runtime.NumCPU(), "cells run in parallel, each in its own process")
	maxWall := fset.Duration("max-wall", 0, "stop launching cells after this much wall time; running cells finish (0: no limit)")
	cellWall := fset.Duration("cell-max-wall", 0, "wall-time limit passed to each cell's run (0: the cell config's maxWall, if any)")
	out := fset.String("out", "", "output directory, overriding the matrix file's outDir")
	only := fset.String("only", "", "comma-separated groups to run (default: all)")
	dryRun := fset.Bool("dry-run", false, "print the expanded run IDs and exit")
	if err := fset.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	switch {
	case fset.NArg() > 0:
		return fmt.Errorf("unexpected arguments %q", fset.Args())
	case *path == "":
		return fmt.Errorf("--matrix is required")
	case *workers < 1:
		return fmt.Errorf("--workers must be at least 1")
	case *maxWall < 0 || *cellWall < 0:
		return fmt.Errorf("wall-time limits must not be negative")
	}
	m, err := loadMatrix(*path)
	if err != nil {
		return err
	}
	cells, err := m.Expand(splitList(*only))
	if err != nil {
		return fmt.Errorf("matrix %s, %w", m.Name, err)
	}
	if *dryRun {
		for _, c := range cells {
			fmt.Fprintln(stdout, c.RunID)
		}
		return nil
	}
	outDir := m.OutDir
	if *out != "" {
		outDir = *out
	}
	// Absolute, because the cell command line names its files and the subprocess must not depend on our directory.
	if outDir, err = filepath.Abs(outDir); err != nil {
		return err
	}
	exe, err := os.Executable()
	if err != nil {
		return fmt.Errorf("locating this binary to run cells, %w", err)
	}
	r := &matrixRunner{name: m.Name, outDir: outDir, workers: *workers, maxWall: *maxWall, cellWall: *cellWall, exe: exe,
		now: time.Now, stdout: stdout}
	_, err = r.run(cells)
	return err
}

func splitList(s string) []string {
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// matrixRunner runs cells as subprocesses of this binary. A process per cell isolates Karpenter's package-level
// state (metrics registries, feature gates, caches) between runs, and lets a cell crash without taking the matrix
// down.
type matrixRunner struct {
	name     string
	outDir   string
	workers  int
	maxWall  time.Duration
	cellWall time.Duration
	exe      string
	// launch runs one prepared cell directory, writing the process's output to log, and returns its exit code; an
	// error means it could not be started. Nil means execCell; tests substitute it to exercise scheduling.
	launch func(dir string, log *os.File) (int, error)
	now    func() time.Time
	stdout io.Writer

	// lock is held for the whole invocation and inherited by every cell process.
	lock *os.File

	mu    sync.Mutex
	index *os.File
	done  int
	total int
}

func (r *matrixRunner) run(cells []Cell) (matrixCounts, error) {
	start := r.now()
	if r.launch == nil {
		r.launch = r.execCell
	}
	if err := os.MkdirAll(r.outDir, 0o755); err != nil {
		return matrixCounts{}, err
	}
	locked, err := r.waitLock(start)
	if err != nil {
		return matrixCounts{}, err
	}
	if !locked {
		return r.finish(cells, true)
	}
	defer func() { _ = r.lock.Close() }()
	if r.index, err = os.OpenFile(filepath.Join(r.outDir, "index.jsonl"), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644); err != nil {
		return matrixCounts{}, err
	}
	defer func() { _ = r.index.Close() }()
	states, err := scanCells(r.outDir, cells)
	if err != nil {
		return matrixCounts{}, err
	}
	before := tally(cells, states)
	r.total, r.done = before.Total, before.Done
	fmt.Fprintf(r.stdout, "matrix %s: %d cells, %d done, %d failed, %d pending; %d workers, wall budget %s, out %s\n",
		r.name, before.Total, before.Done, before.Failed, before.Pending, r.workers, budget(r.maxWall), r.outDir)

	slots := make(chan struct{}, r.workers)
	var wg sync.WaitGroup
	stopped := false
	for _, c := range cells {
		st := states[c.RunID]
		if st.done || st.failures >= maxAttempts {
			continue
		}
		slots <- struct{}{}
		// Checked once a slot is free, so a cell is never launched after the budget, however long the wait was.
		if r.maxWall > 0 && r.now().Sub(start) >= r.maxWall {
			stopped = true
			break
		}
		wg.Add(1)
		go func() {
			defer func() { <-slots; wg.Done() }()
			r.runCell(c, st.failures+1)
		}()
	}
	wg.Wait()
	return r.finish(cells, stopped)
}

// finish rescans rather than trusting this invocation's tally, so the final line also counts cells that orphans of an
// earlier runner completed.
func (r *matrixRunner) finish(cells []Cell, stopped bool) (matrixCounts, error) {
	states, err := scanCells(r.outDir, cells)
	if err != nil {
		return matrixCounts{}, err
	}
	counts := tally(cells, states)
	if stopped && counts.Pending > 0 {
		fmt.Fprintf(r.stdout, "matrix %s: wall budget %s spent; re-invoke to continue\n", r.name, budget(r.maxWall))
	}
	fmt.Fprintf(r.stdout, "matrix %s: %d/%d done, %d failed, %d pending\n", r.name, counts.Done, counts.Total, counts.Failed, counts.Pending)
	return counts, nil
}

func budget(d time.Duration) string {
	if d == 0 {
		return "unlimited"
	}
	return d.String()
}

// waitLock takes the output directory's lock, waiting out another runner for at most the wall budget: two runners
// on one directory would launch the same cells into the same directories.
func (r *matrixRunner) waitLock(start time.Time) (bool, error) {
	path := filepath.Join(r.outDir, ".matrix.lock")
	for waited := false; ; waited = true {
		f, ok, err := tryLock(path)
		if err != nil {
			return false, fmt.Errorf("locking %s, %w", path, err)
		}
		if ok {
			r.lock = f
			return true, nil
		}
		if !waited {
			fmt.Fprintf(r.stdout, "matrix %s: another runner or its cells hold %s; waiting\n", r.name, path)
		}
		if r.maxWall > 0 && r.now().Sub(start) >= r.maxWall {
			return false, nil
		}
		time.Sleep(lockPoll)
	}
}

// runCell makes one attempt at a cell and records it.
func (r *matrixRunner) runCell(c Cell, attempt int) {
	dir := filepath.Join(r.outDir, c.RunID)
	start := r.now()
	code, err := r.attempt(c, dir)
	e := indexEntry{RunID: c.RunID, Group: c.Group, Status: statusDone, ExitCode: code, Attempt: attempt,
		WallSeconds: math.Round(r.now().Sub(start).Seconds()*1000) / 1000}
	if err == nil && code == 0 && !exists(filepath.Join(dir, "summary.json")) {
		err = fmt.Errorf("exited 0 without writing summary.json")
	}
	detail := ""
	if err != nil || code != 0 {
		e.Status = statusFailed
		if attempt >= maxAttempts {
			e.Status = statusAbandoned
		}
		if err != nil {
			e.Error = err.Error()
		}
		detail = r.setAside(dir, attempt, &e)
	}
	e.FinishedAt = r.now().UTC()
	r.record(e, detail)
}

// attempt prepares a cell's directory and runs it.
func (r *matrixRunner) attempt(c Cell, dir string) (int, error) {
	// A directory without summary.json is an interrupted attempt; start over rather than mix its files with ours.
	if err := os.RemoveAll(dir); err != nil {
		return -1, err
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return -1, err
	}
	cfg := maps.Clone(c.Config)
	cfg["runID"] = c.RunID
	if err := writeJSON(filepath.Join(dir, "cell.json"), cfg); err != nil {
		return -1, err
	}
	log, err := os.Create(filepath.Join(dir, "run.log"))
	if err != nil {
		return -1, err
	}
	defer func() { _ = log.Close() }()
	return r.launch(dir, log)
}

// execCell runs the cell in a subprocess of this binary.
func (r *matrixRunner) execCell(dir string, log *os.File) (int, error) {
	cmd := exec.Command(r.exe, "run", "--config", filepath.Join(dir, "cell.json"), "--out", dir, "--max-wall", r.cellWall.String())
	cmd.Stdout, cmd.Stderr = log, log
	if r.lock != nil {
		// Inheriting the lock keeps it held until the last cell exits, so a runner killed mid-matrix cannot be
		// replaced by one that relaunches a cell its orphan is still writing.
		cmd.ExtraFiles = []*os.File{r.lock}
	}
	err := cmd.Run()
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		return exitErr.ExitCode(), nil
	}
	if err != nil {
		return -1, err
	}
	return 0, nil
}

// setAside renames a failed attempt's directory to <runID>.failed-<n>, which is how the next invocation knows to
// retry it and how many attempts it has had. It returns the progress line's detail.
func (r *matrixRunner) setAside(dir string, attempt int, e *indexEntry) string {
	kept := ""
	for n := attempt; ; n++ {
		if kept = dir + failedInfix + strconv.Itoa(n); !exists(kept) {
			break
		}
	}
	var err error
	if exists(dir) {
		err = os.Rename(dir, kept)
	} else {
		// Setup failed before the directory existed; the attempt must still count.
		err = os.MkdirAll(kept, 0o755)
	}
	if err != nil && e.Error == "" {
		e.Error = fmt.Sprintf("setting aside the failed attempt, %v", err)
	}
	detail := fmt.Sprintf(", exit %d, attempt %d of %d, log %s", e.ExitCode, e.Attempt, maxAttempts, filepath.Join(kept, "run.log"))
	if e.Error != "" {
		detail += ": " + e.Error
	}
	if e.Status == statusFailed {
		detail += "; retried on the next invocation"
	}
	return detail
}

// record appends the attempt to the index in a single write, so lines from parallel cells never interleave.
func (r *matrixRunner) record(e indexEntry, detail string) {
	line, err := json.Marshal(e)
	if err != nil {
		panic(err) // indexEntry always encodes
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, err := r.index.Write(append(line, '\n')); err != nil {
		fmt.Fprintf(os.Stderr, "matrix %s: appending to index.jsonl, %v\n", r.name, err)
	}
	if e.Status == statusDone {
		r.done++
	}
	fmt.Fprintf(r.stdout, "[%d/%d done] %s %s in %.1fs%s\n", r.done, r.total, e.Status, e.RunID, e.WallSeconds, detail)
}

type cellState struct {
	done     bool
	failures int
}

// scanCells reads each cell's state from the output directory: done once summary.json exists, and failed as many
// times as it has .failed-<n> directories.
func scanCells(outDir string, cells []Cell) (map[string]cellState, error) {
	entries, err := os.ReadDir(outDir)
	if err != nil && !errors.Is(err, fs.ErrNotExist) {
		return nil, err
	}
	failures := map[string]int{}
	for _, e := range entries {
		id, n, ok := strings.Cut(e.Name(), failedInfix)
		if _, err := strconv.ParseUint(n, 10, 32); ok && e.IsDir() && err == nil {
			failures[id]++
		}
	}
	states := make(map[string]cellState, len(cells))
	for _, c := range cells {
		states[c.RunID] = cellState{done: exists(filepath.Join(outDir, c.RunID, "summary.json")), failures: failures[c.RunID]}
	}
	return states, nil
}

func tally(cells []Cell, states map[string]cellState) matrixCounts {
	counts := matrixCounts{Total: len(cells)}
	for _, c := range cells {
		switch st := states[c.RunID]; {
		case st.done:
			counts.Done++
		case st.failures >= maxAttempts:
			counts.Failed++
		default:
			counts.Pending++
		}
	}
	return counts
}

func exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}
