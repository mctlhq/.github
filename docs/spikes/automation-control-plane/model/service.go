package model

import (
	"fmt"
	"strings"
	"time"
)

// Caller is the already-authenticated caller. Principal is the canonical
// principal; Surface is where the call arrived. BoundTenant is set only for
// the tenant-bound support actor.
type Caller struct {
	Principal, Surface, Actor, BoundTenant string
}

type AgentFunc func(d *Definition) (string, error)

// Service is the control-plane logic that would live in mctl-api. It holds no
// durable state of its own: everything is in Stores (restart-safe).
type Service struct {
	S         *Stores
	Clock     Clock
	Templates map[string]Template
	Agent     AgentFunc
	Telegram  DeliveryAdapter
	AutoRun   bool // run executions to completion at fire time
	// Test hooks.
	CrashAfter               map[string]bool // one-shot: phase name
	BeforeAction             func()          // runs right before the action-time checkpoint
	CrashAfterTemporalCreate bool
	AgentCalls               int
	Verifier                 *Verifier
	RunCostMinor             int64
	MaxRefusals              int
	DeliveryTries            int
}

func NewService(s *Stores, c Clock, tpl map[string]Template) *Service {
	return &Service{S: s, Clock: c, Templates: tpl, AutoRun: true, CrashAfter: map[string]bool{},
		RunCostMinor: 10, MaxRefusals: 3, DeliveryTries: 3,
		Agent: func(*Definition) (string, error) { return "result", nil }}
}

func (v *Service) authorize(c Caller, tenant string) error {
	if !v.S.Authority.Linked(c.Principal, c.Surface) {
		return ErrIdentityUnlinked
	}
	if c.Actor == "support" {
		if c.BoundTenant != tenant {
			return ErrNotFound
		}
		return nil
	}
	v.S.Authority.mu.Lock()
	ok := v.S.Authority.members[tenant+"|"+c.Principal]
	v.S.Authority.mu.Unlock()
	if !ok {
		return ErrNotFound // not-found semantics: no existence oracle
	}
	return nil
}

func reqHash(r CreateRequest) string { r.IdempotencyKey = ""; return fmt.Sprintf("%+v", r) }

// Create persists DESIRED state first, then applies it idempotently to Temporal.
func (v *Service) Create(c Caller, r CreateRequest) (*Definition, error) {
	if r.WorkflowType != "" || r.TaskQueue != "" || r.Namespace != "" || r.Signal != "" {
		return nil, ErrRawTemporal
	}
	if r.IdempotencyKey == "" {
		return nil, ErrInvalid
	}
	if err := v.authorize(c, r.Tenant); err != nil {
		return nil, err
	}
	tpl, ok := v.Templates[r.Template]
	if !ok {
		return nil, ErrInvalid
	}
	v.S.mu.Lock()
	if rec, ok := v.S.Idem[c.Principal+"|"+r.IdempotencyKey]; ok {
		defer v.S.mu.Unlock()
		if rec.hash != reqHash(r) {
			return nil, ErrIdempotencyReused
		}
		return v.S.Defs[rec.id], nil
	}
	v.S.mu.Unlock()
	if c.Actor == "support" {
		if !tpl.Support || tpl.Mutating || r.TTL <= 0 || r.TTL > 24*time.Hour || r.MaxFires < 1 || r.MaxFires > 3 || r.AuthRef == "" {
			return nil, ErrTemplateForbidden
		}
	} else if c.Principal != r.Owner {
		return nil, ErrNotFound
	}
	d := &Definition{Tenant: r.Tenant, Owner: r.Owner, Surface: c.Surface, Actor: c.Actor, Template: r.Template,
		Trigger: r.Trigger, Install: r.Install, InstallRev: r.InstallRev, Budget: r.Budget, State: StateEnabled,
		Version: 1, MaxFires: r.MaxFires, ReadOnly: c.Actor == "support" || !tpl.Mutating, DeliveryChat: r.DeliveryChat, AuthRef: r.AuthRef}
	if r.TTL > 0 {
		d.ExpiresAt = v.Clock.Now().Add(r.TTL)
	}
	v.S.mu.Lock()
	d.ID = v.S.id("au")
	v.S.Defs[d.ID] = d
	v.S.Idem[c.Principal+"|"+r.IdempotencyKey] = idemRec{reqHash(r), d.ID}
	v.S.Desired[d.ID] = &Desired{AutomationID: d.ID, Version: 1}
	v.S.mu.Unlock()
	return d, v.apply(d.ID)
}

// apply is the idempotent Temporal apply step of the desired/observed protocol.
func (v *Service) apply(au string) error {
	v.S.mu.Lock()
	d, des := v.S.Defs[au], v.S.Desired[au]
	v.S.mu.Unlock()
	if d == nil || des == nil {
		return ErrNotFound
	}
	if d.Trigger.Kind == TrigAt || d.Trigger.Kind == TrigSchedule {
		if d.State == StateDeleted {
			v.S.Temporal.Delete(ScheduleID(au))
		} else {
			v.S.Temporal.Create(ScheduleID(au), au, v.Clock.Now())
			if s, ok := v.S.Temporal.Get(ScheduleID(au)); ok {
				s.Paused = d.State == StatePaused
			}
		}
	}
	if v.CrashAfterTemporalCreate {
		v.CrashAfterTemporalCreate = false
		return ErrCrash // Temporal accepted; MCTL never recorded success
	}
	v.S.mu.Lock()
	des.Applied = true
	v.S.mu.Unlock()
	return nil
}

// Reconcile converges observed Temporal state to desired state.
// Orphans (schedule with no desired record) are deleted only when the
// observation is complete; an unreadable Temporal is "unknown", never "absent".
func (v *Service) Reconcile(observedComplete bool) (repaired, orphans int) {
	v.S.mu.Lock()
	var pend []string
	for id, des := range v.S.Desired {
		if !des.Applied {
			pend = append(pend, id)
		}
	}
	v.S.mu.Unlock()
	for _, id := range pend {
		_ = v.apply(id)
		repaired++
	}
	if !observedComplete {
		return
	}
	for _, s := range v.S.Temporal.List() {
		v.S.mu.Lock()
		_, ok := v.S.Desired[s.Automation]
		v.S.mu.Unlock()
		if !ok {
			v.S.Temporal.Delete(s.ID)
			orphans++
		}
	}
	return
}

func (v *Service) Get(c Caller, id string) (*Definition, error) {
	v.S.mu.Lock()
	d := v.S.Defs[id]
	v.S.mu.Unlock()
	if d == nil {
		return nil, ErrNotFound
	}
	if err := v.authorize(c, d.Tenant); err != nil {
		return nil, err
	}
	if c.Actor == "support" && !v.Clock.Now().Before(d.ExpiresAt) {
		return nil, ErrNotFound // support read is TTL-bounded
	}
	return d, nil
}

func (v *Service) List(c Caller, tenant string) ([]*Definition, error) {
	if err := v.authorize(c, tenant); err != nil {
		return nil, err
	}
	v.S.mu.Lock()
	defer v.S.mu.Unlock()
	var out []*Definition
	for _, d := range v.S.Defs {
		if d.Tenant == tenant && d.State != StateDeleted {
			out = append(out, d)
		}
	}
	return out, nil
}

func (v *Service) mutate(c Caller, id string, expected int, f func(*Definition)) (*Definition, error) {
	d, err := v.Get(c, id)
	if err != nil {
		return nil, err
	}
	if c.Actor == "support" {
		return nil, ErrTemplateForbidden
	}
	if expected != d.Version {
		return nil, ErrVersionConflict
	}
	v.S.mu.Lock()
	f(d)
	d.Version++
	v.S.Desired[id].Version, v.S.Desired[id].Applied = d.Version, false
	v.S.mu.Unlock()
	return d, v.apply(id)
}

func (v *Service) Pause(c Caller, id string, ver int) (*Definition, error) {
	return v.mutate(c, id, ver, func(d *Definition) { d.State = StatePaused })
}
func (v *Service) Resume(c Caller, id string, ver int) (*Definition, error) {
	return v.mutate(c, id, ver, func(d *Definition) { d.State = StateEnabled; d.Refusals = 0 })
}

// Delete stops future fires; in-flight runs finish unless cancelInFlight.
func (v *Service) Delete(c Caller, id string, ver int, cancelInFlight bool) (*Definition, error) {
	d, err := v.mutate(c, id, ver, func(d *Definition) { d.State = StateDeleted })
	if err == nil && cancelInFlight {
		for _, e := range v.S.Running(id) {
			e.Phase, e.Reason = "cancelled", "cancel_in_flight"
			v.S.Ledger.Release(e.ID, d.Owner)
		}
	}
	return d, err
}

// Fire starts (or idempotently reuses) the per-occurrence WorkItem + execution.
func (v *Service) Fire(au, occ string) (*Execution, error) {
	v.S.mu.Lock()
	d := v.S.Defs[au]
	v.S.mu.Unlock()
	if d == nil || d.State != StateEnabled {
		return nil, ErrNotFound
	}
	w, _ := v.S.EnsureWorkItem(au, occ)
	e, fresh := v.S.StartExecution(w)
	if !fresh || len(w.Executions) > 1 || e.Phase != "queued" {
		return e, nil // replay of the same occurrence
	}
	if !d.ExpiresAt.IsZero() && !v.Clock.Now().Before(d.ExpiresAt) || d.MaxFires > 0 && d.Fires >= d.MaxFires {
		e.Phase, e.Reason = "refused", "expired"
		return e, nil
	}
	if err := v.S.Authority.Check(d); err != nil { // fire-time checkpoint
		e.Phase, e.Reason = "refused", err.Error()
		d.Refusals++
		if d.Refusals >= v.MaxRefusals {
			d.State = StatePaused // auto-pause on repeated refusals
		}
		return e, err
	}
	d.Refusals = 0
	d.Fires++
	if err := v.S.Ledger.Reserve(e.ID, d.Owner, v.RunCostMinor, d.Budget); err != nil {
		e.Phase, e.Reason = "refused", err.Error() // before the agent mock is called
		return e, err
	}
	e.Phase = "reserved"
	if v.CrashAfter["reserved"] {
		delete(v.CrashAfter, "reserved")
		return e, ErrCrash
	}
	if v.AutoRun {
		return e, v.Advance(e.ID)
	}
	return e, nil
}

// Advance drives an execution through its phases idempotently, so a restarted
// worker resumes the SAME we_ without a second reservation.
func (v *Service) Advance(weID string) error {
	e := v.S.ByID[weID]
	var d *Definition
	for _, w := range v.S.WorkItems {
		if w.ID == e.WorkItem {
			d = v.S.Defs[w.Automation]
		}
	}
	for !e.Terminal() {
		switch e.Phase {
		case "reserved":
			if err := v.S.Ledger.Reserve(e.ID, d.Owner, v.RunCostMinor, d.Budget); err != nil {
				return err
			}
			if _, err := v.Agent(d); err != nil {
				e.Phase, e.Reason = "failed", "agent_error"
				v.S.Ledger.Release(e.ID, d.Owner)
				return err
			}
			v.AgentCalls++
			e.AgentCalls++
			e.Phase = "agent_done"
		case "agent_done":
			v.S.Ledger.Settle(e.ID)
			e.Phase = "settled"
		case "settled":
			if v.BeforeAction != nil {
				v.BeforeAction()
			}
			if err := v.S.Authority.Check(d); err != nil { // action-time checkpoint
				e.Phase, e.Reason = "refused", "action_time:"+err.Error()
				return err
			}
			if d.DeliveryChat != "" && v.Telegram != nil {
				e.Delivery = Deliver(v.Telegram, d.DeliveryChat, "automation "+d.ID+" result", v.DeliveryTries)
			}
			e.Phase = "done"
		default:
			return nil // queued / awaiting_signal
		}
		if v.CrashAfter[e.Phase] {
			delete(v.CrashAfter, e.Phase)
			return ErrCrash
		}
	}
	return nil
}

// Tick evaluates every observed schedule against the fake clock.
func (v *Service) Tick() {
	now := v.Clock.Now()
	for _, s := range v.S.Temporal.List() {
		v.S.mu.Lock()
		d, des := v.S.Defs[s.Automation], v.S.Desired[s.Automation]
		v.S.mu.Unlock()
		if d == nil || des == nil || s.Paused || s.Done || d.State != StateEnabled {
			continue // an orphan or paused schedule never fires
		}
		occs := d.Trigger.Occurrences(s.LastScan, now)
		s.LastScan = now
		var inWin []time.Time
		for _, o := range occs {
			if d.Trigger.CatchUp == 0 || now.Sub(o) <= d.Trigger.CatchUp {
				inWin = append(inWin, o)
			} // else: misfire outside the catch-up window is dropped
		}
		if d.Trigger.Kind == TrigSchedule && len(inWin) > 1 {
			inWin = inWin[len(inWin)-1:] // misfires inside the window run once
		}
		for _, o := range inWin {
			if d.Trigger.Kind == TrigAt {
				s.Done = true
			}
			running := v.S.Running(d.ID)
			switch {
			case len(running) > 0 && d.Trigger.Overlap == OverlapSkip || len(running) > 0 && d.Trigger.Overlap == "":
				s.Skipped++
				continue
			case len(running) > 0 && d.Trigger.Overlap == OverlapBufferOne:
				o2 := o
				s.Buffered = &o2
				continue
			case len(running) > 0 && d.Trigger.Overlap == OverlapCancelOther:
				for _, r := range running {
					r.Phase, r.Reason = "cancelled", "overlap_cancel_other"
				}
			}
			_, _ = v.Fire(d.ID, "occ-"+o.UTC().Format(time.RFC3339))
		}
	}
}

// Ingest authenticates, dedupes, routes one event, and returns what it did.
func (v *Service) Ingest(ts time.Time, sig string, body []byte, tenant string) (decision string, err error) {
	e, err := v.Verifier.Verify(ts, sig, body)
	if err != nil {
		return "", err
	}
	if !supportedNow(e.Type) {
		return "", ErrUnsupportedEvent
	}
	v.S.mu.Lock()
	if v.S.Seen[e.ID] {
		v.S.mu.Unlock()
		return "duplicate", ErrDuplicate
	}
	v.S.Seen[e.ID] = true
	var ds []*Definition
	for _, d := range v.S.Defs {
		if d.Tenant == tenant && d.State == StateEnabled && (d.Trigger.Kind == TrigEvent || d.Trigger.Kind == TrigDependency) &&
			d.Trigger.EventType == e.Type && strings.HasPrefix(e.Subject["repo"], d.Trigger.SubjectPrefix) {
			ds = append(ds, d)
		}
	}
	v.S.mu.Unlock()
	capped := 0
	for _, d := range ds {
		if d.Trigger.Await { // dependency template: Signal the awaiting run
			for _, r := range v.S.Running(d.ID) {
				if r.Phase == "awaiting_signal" {
					r.Signals++
					r.Phase = "reserved"
					decision = "signal"
					err = v.Advance(r.ID)
					return
				}
			}
		}
		if cap := d.Trigger.FanoutCap; cap > 0 && len(v.S.Running(d.ID)) >= cap {
			capped++
			continue
		}
		if _, ferr := v.Fire(d.ID, "ev-"+e.ID); ferr == nil {
			decision = "start"
		}
	}
	if decision == "" && capped > 0 {
		return "coalesced", ErrFanoutCapped
	}
	return decision, nil
}

// Run is mctl_run_automation (manual fire); dependency/await templates park.
func (v *Service) Run(c Caller, id, idem string) (*Execution, error) {
	d, err := v.Get(c, id)
	if err != nil {
		return nil, err
	}
	auto := v.AutoRun
	if d.Trigger.Await {
		v.AutoRun = false
	}
	e, err := v.Fire(id, "manual-"+idem)
	v.AutoRun = auto
	if d.Trigger.Await && err == nil && e.Phase == "reserved" {
		e.Phase = "awaiting_signal"
	}
	return e, err
}
