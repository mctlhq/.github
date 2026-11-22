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
			owner, clientPolicy, clientID := "", "", ""
			if task, ok, err := s.Task(x.TaskKey); err == nil && ok {
				owner, clientPolicy, clientID = task.Owner, task.ClientPolicy, task.ClientID
			}
			t.Start(we, phases, []string{"alice"}, owner, clientPolicy, clientID)
			if s.CancelRecorded(x.ID) { // cancel raced the claim: honor it now
				t.RequestCancel(we)
			}
			n++
		}
	}
	return n
}

// Describe is a debugging helper.
func (w Workflow) String() string { return fmt.Sprintf("%s idx=%d gate=%s", w.State, w.Idx, w.Gate) }
