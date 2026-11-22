package model

import (
	"fmt"
	"strings"
)

// Dispatch is the fake execution-request dispatcher: the only start path.
// It claims every pending xr_ and starts the probe workflow.
func Dispatch(s *Store, t *Temporal) int {
	n := 0
	for _, x := range s.PendingXRs() {
		we := "we_" + strings.TrimPrefix(x.ID, "xr_")
		if s.Claim(x.ID, we) {
			phases := strings.Split(x.Scenario, ",")
			if x.Scenario == "" {
				phases = []string{"sleep"}
			}
			t.Start(we, phases, []string{"alice"})
			n++
		}
	}
	return n
}

// Describe is a debugging helper.
func (w Workflow) String() string { return fmt.Sprintf("%s idx=%d gate=%s", w.State, w.Idx, w.Gate) }
