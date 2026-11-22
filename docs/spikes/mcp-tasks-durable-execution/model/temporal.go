package model

import (
	"errors"
	"fmt"
	"sync"
	"time"
)

// Gate states mirror the ADR 013 / ApprovalWaitWorkflow vocabulary.
const (
	GateNone     = ""
	GateInput    = "WAITING_FOR_INPUT"
	GateApproval = "WAITING_FOR_APPROVAL"
	GateTimedOut = "INPUT_TIMED_OUT"
)

// HIRequest is a sealed ADR 013-shaped human-input request.
type HIRequest struct {
	RequestID, Hash string
	Message         string
	MaxLen          int // typed ResponseSpec: string up to MaxLen
	ActorRefs       []string
	AllowMCP        bool // respondent_policy allows mcp/client_relayed (ADR 013 extension, G4)
	ExpiresAt       time.Time
	Round, MaxRound int
}

// HIResponse is what SignalHumanInputResponse carries.
type HIResponse struct {
	RequestID, RequestHash, Value          string
	Respondent, Surface, Assurance, Client string
}

// Grant is an aar_ approval grant on the decide page.
type Grant struct {
	ID, ArtifactHash, ContextDigest string
	State                           string // pending | approved | consumed
	Deciders                        map[string]bool
}

// Workflow is the fake TaskProbeWorkflow. Phases run in order.
type Workflow struct {
	ID           string
	Phases       []string // sleep | input | human_input | approval
	Idx          int
	State        string // Running | Completed | Failed | Canceled | TimedOut | Terminated | OrphanClosed
	Gate         string
	Req          *HIRequest
	Grant        *Grant
	CancelWanted bool
	Business     bool // completed with isError
	Rejections   []string
	Answers      []string
	Actors       []string
}

// Temporal is the fake Temporal client with a signal spy.
type Temporal struct {
	mu         sync.Mutex
	Now        func() time.Time
	FailReads  bool
	WFs        map[string]*Workflow
	Signals    []string // spy: every signal name received
	BeforeSig  func(*Workflow)
	Grants     map[string]*Grant
	GrantCount int
}

// NewTemporal returns an empty fake.
func NewTemporal(now func() time.Time) *Temporal {
	return &Temporal{Now: now, WFs: map[string]*Workflow{}, Grants: map[string]*Grant{}}
}

var errTemporal = errors.New("temporal unreadable")

// Start begins a workflow for a scenario ("," separated phases).
func (t *Temporal) Start(id string, phases []string, actors []string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	w := &Workflow{ID: id, Phases: phases, State: "Running", Actors: actors}
	t.WFs[id] = w
	t.enter(w)
}

func (t *Temporal) enter(w *Workflow) {
	w.Gate, w.Req, w.Grant = GateNone, nil, nil
	if w.Idx >= len(w.Phases) {
		w.State = "Completed"
		return
	}
	switch p := w.Phases[w.Idx]; p {
	case "input", "human_input":
		w.Gate = GateInput
		h := fmt.Sprintf("sha256:%016x%016x", w.Idx+1, len(w.ID))
		w.Req = &HIRequest{RequestID: "hir-" + h[7:23], Hash: h, Message: "Enter a value", MaxLen: 64,
			ActorRefs: w.Actors, AllowMCP: p == "input", ExpiresAt: t.Now().Add(72 * time.Hour), MaxRound: 3, Round: 1}
	case "approval":
		w.Gate = GateApproval
		t.GrantCount++
		g := &Grant{ID: fmt.Sprintf("aar_%d", t.GrantCount), ArtifactHash: "sha256:art", ContextDigest: "sha256:ctx",
			State: "pending", Deciders: map[string]bool{"owner-1": true}}
		t.Grants[g.ID] = g
		w.Grant = g
	}
}

// Describe returns a copy of workflow state.
func (t *Temporal) Describe(id string) (Workflow, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.FailReads {
		return Workflow{}, errTemporal
	}
	w, ok := t.WFs[id]
	if !ok {
		return Workflow{}, errTemporal
	}
	if w.State == "Running" && w.Req != nil && t.Now().After(w.Req.ExpiresAt) {
		c := *w
		c.Gate = GateTimedOut
		return c, nil
	}
	return *w, nil
}

// SignalHumanInput delivers an answer; the workflow re-validates it
// (ADR 013 validate_response) and never trusts the adapter.
func (t *Temporal) SignalHumanInput(id string, r HIResponse) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.Signals = append(t.Signals, "human_input_response")
	w := t.WFs[id]
	if w == nil || w.State != "Running" || w.Req == nil {
		return
	}
	if t.BeforeSig != nil {
		t.BeforeSig(w)
	}
	switch {
	case r.RequestID != w.Req.RequestID:
		w.Rejections = append(w.Rejections, "request_id_mismatch")
	case r.RequestHash != w.Req.Hash:
		w.Rejections = append(w.Rejections, "hash_mismatch")
	case t.Now().After(w.Req.ExpiresAt):
		w.Rejections = append(w.Rejections, "expired")
	case !contains(w.Req.ActorRefs, r.Respondent):
		w.Rejections = append(w.Rejections, "respondent_mismatch")
	default:
		w.Answers = append(w.Answers, r.Value)
		if r.Value == "fail" {
			w.Business = true
		}
		w.Idx++
		t.enter(w)
	}
}

// RequestCancel asks the workflow to cancel. A terminal workflow ignores it.
// Gates cancel at once; an activity (sleep) cancels at the next Tick.
func (t *Temporal) RequestCancel(id string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.Signals = append(t.Signals, "cancel")
	w := t.WFs[id]
	if w == nil || w.State != "Running" {
		return
	}
	w.CancelWanted = true
	if w.Gate != GateNone {
		w.State = "Canceled"
	}
}

// DecideGrant is the decide page: fresh sign-in, bound to exact content.
func (t *Temporal) DecideGrant(by, id, artifact, ctx string) string {
	t.mu.Lock()
	defer t.mu.Unlock()
	g := t.Grants[id]
	if g == nil || !g.Deciders[by] {
		return "forbidden"
	}
	if g.ArtifactHash != artifact || g.ContextDigest != ctx {
		return "mismatch"
	}
	if g.State != "pending" {
		return "consumed"
	}
	g.State = "approved"
	return "approved"
}

// Tick advances time-driven phases: sleep completes, approved grants are
// consumed exactly once, a wanted cancel lands at the activity boundary.
func (t *Temporal) Tick() {
	t.mu.Lock()
	defer t.mu.Unlock()
	for _, w := range t.WFs {
		if w.State != "Running" {
			continue
		}
		if w.CancelWanted {
			w.State = "Canceled"
			continue
		}
		if w.Idx >= len(w.Phases) {
			continue
		}
		switch w.Phases[w.Idx] {
		case "sleep":
			w.Idx++
			t.enter(w)
		case "approval":
			if w.Grant != nil && w.Grant.State == "approved" {
				w.Grant.State = "consumed"
				w.Idx++
				t.enter(w)
			}
		}
	}
}

// Force sets a terminal state (test hook for Failed/TimedOut/Terminated).
func (t *Temporal) Force(id, state string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if w := t.WFs[id]; w != nil && w.State == "Running" {
		w.State = state
	}
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}
