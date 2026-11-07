package model

import (
	"fmt"
	"sort"
	"sync"
	"time"
)

// WorkItem/Execution/Request stand in for the canonical mctl-api records
// (wi_, xr_, we_). AutomationRun is a VIEW over these, not a second store.
type Execution struct {
	ID, WorkItem, Request string
	Phase                 string // queued|reserved|awaiting_signal|agent_done|settled|done|refused|cancelled|failed
	Reason                string
	Delivery              DeliveryResult
	Signals               int
	AgentCalls            int
}

func (e *Execution) Terminal() bool {
	return e.Phase == "done" || e.Phase == "refused" || e.Phase == "cancelled" || e.Phase == "failed"
}

type WorkItem struct {
	ID, Automation, Key string
	Executions          []*Execution
}

// Stores is the shared durable state; a model "restart" builds a new Service on the same Stores.
type Stores struct {
	mu        sync.Mutex
	Defs      map[string]*Definition
	Idem      map[string]idemRec
	WorkItems map[string]*WorkItem // by key (au|occurrence)
	ByID      map[string]*Execution
	Seen      map[string]bool // verified event ids
	Desired   map[string]*Desired
	Temporal  *TemporalSim
	Ledger    *Ledger
	Authority *Authority
	n         int
}

type idemRec struct {
	hash string
	id   string
}

// Desired is the persisted-first intent for the Postgres<->Temporal protocol.
type Desired struct {
	AutomationID string
	Version      int
	Applied      bool
}

func NewStores() *Stores {
	return &Stores{Defs: map[string]*Definition{}, Idem: map[string]idemRec{}, WorkItems: map[string]*WorkItem{},
		ByID: map[string]*Execution{}, Seen: map[string]bool{}, Desired: map[string]*Desired{},
		Temporal: NewTemporalSim(), Ledger: NewLedger(), Authority: NewAuthority()}
}

func (s *Stores) id(prefix string) string { s.n++; return fmt.Sprintf("%s_%04d", prefix, s.n) }

// EnsureWorkItem is idempotent on key: one wi_ per (automation, occurrence).
func (s *Stores) EnsureWorkItem(au, occ string) (*WorkItem, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	key := au + "|" + occ
	if w, ok := s.WorkItems[key]; ok {
		return w, false
	}
	w := &WorkItem{ID: s.id("wi"), Automation: au, Key: key}
	s.WorkItems[key] = w
	return w, true
}

// StartExecution enforces: at most one non-terminal we_ per wi_.
func (s *Stores) StartExecution(w *WorkItem) (*Execution, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, e := range w.Executions {
		if !e.Terminal() {
			return e, false
		}
	}
	e := &Execution{ID: s.id("we"), WorkItem: w.ID, Request: s.id("xr"), Phase: "queued"}
	w.Executions = append(w.Executions, e)
	s.ByID[e.ID] = e
	return e, true
}

// Running returns non-terminal executions of an automation.
func (s *Stores) Running(au string) []*Execution {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []*Execution
	for _, w := range s.WorkItems {
		if w.Automation != au {
			continue
		}
		for _, e := range w.Executions {
			if !e.Terminal() {
				out = append(out, e)
			}
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

// Run is the derived AutomationRun view.
type Run struct{ WorkItem, Execution, Phase, Reason, Delivered string }

func (s *Stores) Runs(au string) []Run {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []Run
	for _, w := range s.WorkItems {
		if w.Automation == au {
			for _, e := range w.Executions {
				out = append(out, Run{w.ID, e.ID, e.Phase, e.Reason, e.Delivery.Status})
			}
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Execution < out[j].Execution })
	return out
}

// TemporalSim models only the Schedule surface MCTL would use. It is NOT
// Temporal; behavior that matters is re-measured on a real dev server.
type TemporalSim struct {
	mu      sync.Mutex
	Scheds  map[string]*Sched
	Creates int
}
type Sched struct {
	ID, Automation string
	Paused         bool
	LastScan       time.Time
	Done           bool // `at` one-shot consumed (remaining_actions = 0)
	Skipped        int
	Buffered       *time.Time
}

func NewTemporalSim() *TemporalSim { return &TemporalSim{Scheds: map[string]*Sched{}} }

// Create is idempotent on the deterministic schedule id.
func (t *TemporalSim) Create(id, au string, from time.Time) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if _, ok := t.Scheds[id]; ok {
		return
	}
	t.Scheds[id] = &Sched{ID: id, Automation: au, LastScan: from}
	t.Creates++
}
func (t *TemporalSim) Delete(id string) { t.mu.Lock(); delete(t.Scheds, id); t.mu.Unlock() }
func (t *TemporalSim) Get(id string) (*Sched, bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	s, ok := t.Scheds[id]
	return s, ok
}
func (t *TemporalSim) List() []*Sched {
	t.mu.Lock()
	defer t.mu.Unlock()
	var out []*Sched
	for _, s := range t.Scheds {
		out = append(out, s)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

func ScheduleID(au string) string { return "sch-" + au }
