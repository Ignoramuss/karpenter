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

// consolidation-sim runs the Tier A consolidation simulator.
//
//	go run ./hack/consolidation-sim run --config cell.yaml --out experiments/runs/<run-id>
//	go run ./hack/consolidation-sim run --scenario W1 --nodes 50 --arm B2-when-empty-or-underutilized --seed 1 --out /tmp/run
//	go run ./hack/consolidation-sim matrix --matrix experiments/matrix/session.yaml --workers 4 --max-wall 20m
//	go run ./hack/consolidation-sim catalog > experiments/catalog.json
//
// "matrix" runs pending cells of a matrix file, each in its own process, appends results, and exits; re-invoke it
// until it reports the matrix complete.
package main

import (
	"fmt"
	"os"
	"runtime/debug"
)

// gcPercent trades memory for CPU: a run allocates heavily (Karpenter deep-copies cluster state for every scheduling
// simulation), and at the default GOGC=100 garbage collection took about a third of a run's CPU.
const gcPercent = 400

func main() {
	if os.Getenv("GOGC") == "" {
		debug.SetGCPercent(gcPercent)
	}
	if len(os.Args) < 2 {
		usage()
	}
	var err error
	switch os.Args[1] {
	case "run":
		err = runCmd(os.Args[2:])
	case "matrix":
		err = matrixCmd(os.Args[2:])
	case "strategies":
		err = strategiesCmd()
	case "catalog":
		err = catalogCmd(os.Args[2:])
	default:
		usage()
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: consolidation-sim run|matrix|strategies|catalog [flags]")
	os.Exit(2)
}
