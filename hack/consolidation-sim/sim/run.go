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

package sim

import (
	"fmt"
	"io"
	"time"
)

// Run executes the simulation and writes the timeseries. The decision log streams to the writer given to New. Pod
// sets for the offline lower bound are kept and written by WritePodSets.
func (s *Sim) Run(timeseries io.Writer) (Summary, error) {
	wallStart := time.Now() //nolint:forbidigo // wall time bounds the run; it never reaches decisions
	if err := s.seed(); err != nil {
		return Summary{}, fmt.Errorf("seeding workload, %w", err)
	}
	if len(s.methods) > 0 {
		s.atDeferred(s.start.Add(s.cfg.Warmup.Duration), "disruption", s.disruptionTick)
	}
	if s.cfg.Arm.Name == ArmDescheduler {
		s.at(s.start.Add(s.cfg.Warmup.Duration), "descheduler", s.descheduleTick)
	}
	var sampleTick func()
	sampleTick = func() {
		s.m.sample()
		s.after(s.cfg.MetricsInterval.Duration, "sample", sampleTick)
	}
	s.at(s.start, "sample", sampleTick)
	var podSetTick func()
	podSetTick = func() {
		s.m.recordPodSet()
		s.after(podSetInterval, "podset", podSetTick)
	}
	s.at(s.start.Add(s.cfg.Warmup.Duration), "podset", podSetTick)
	end := s.start.Add(s.cfg.Duration.Duration)
	timedOut := false
	for step := s.start; step.Before(end) && s.stopErr == nil; {
		step = step.Add(time.Minute)
		if step.After(end) {
			step = end
		}
		s.runUntil(step)
		if s.cfg.MaxWall.Duration > 0 && time.Since(wallStart) > s.cfg.MaxWall.Duration {
			timedOut = true
			break
		}
	}
	s.m.sample()
	s.m.recordPodSet()
	sum := s.m.summary(s)
	sum.WallSeconds = time.Since(wallStart).Seconds()
	sum.TimedOut = timedOut
	if timedOut {
		sum.Invalid = true
		sum.InvalidReasons = append(sum.InvalidReasons, "exceeded max wall time")
	}
	if s.stopErr != nil {
		sum.Error = s.stopErr.Error()
		sum.Invalid = true
		sum.InvalidReasons = append(sum.InvalidReasons, "simulation error")
	}
	if s.pipe != nil {
		sum.FullSimulations, sum.SearchTimeouts = s.pipe.SearchTotals()
	}
	if err := s.m.writeTimeseries(timeseries); err != nil {
		return sum, err
	}
	return sum, nil
}

// WritePodSets writes the pod multisets recorded during Run, one JSON object per line.
func (s *Sim) WritePodSets(w io.Writer) error { return s.m.writePodSets(w) }
