// Package model is an offline, stdlib-only reference model for
// mctlhq/.github#41 (MCP Tasks over durable executions). Everything here is
// tagged [MODEL]: it shows commit-order and mapping invariants against fakes.
// It does NOT prove Postgres atomicity, advisory-lock behaviour or index
// backstops; that is gate G1 and is not run.
package model

import (
	"crypto/rand"
	"encoding/base32"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"
)

var (
	// ErrCrash is an injected process crash at a step boundary.
	ErrCrash = errors.New("injected crash")
	// ErrKeyReused is idempotency_key_reused: same scope, different digest.
	ErrKeyReused = errors.New("idempotency_key_reused")
	// ErrStore is an unreadable canonical store.
	ErrStore = errors.New("store unreadable")
)

// Retention knobs (design section 10).
const (
	TerminalRetention = 24 * time.Hour
	XRReplayWindow    = 24 * time.Hour
)

// XR is an execution request (xr_).
type XR struct {
	ID, WIID, Scenario, TaskKey string
	State                       string // pending | claimed | rejected
	Reason                      string
	WEID                        string
}

// TaskRow is the mcp_tasks protocol binding. It has no status column.
type TaskRow struct {
	TaskID, Tenant, Owner, ClientID, Tool string
	ArgsDigest, Key                       string
	WIID, XRID                            string
	ClientPolicy                          string // any_client_of_owner | same_client
	CreatedAt                             time.Time
	TerminalObservedAt                    *time.Time
	CancelRequestedAt                     *time.Time
}

// Scope is the mcp_task_scopes idempotency row.
type Scope struct {
	TaskID, ArgsDigest, State string // State: reserving | bound
	WIID, XRID                string
	Tenant, Owner, ClientID   string
	Tool, Key                 string
	ClientPolicy, Scenario    string
	CreatedAt                 time.Time
	ExpiresAt                 *time.Time
}

// TaskIn is the input of CreateTask.
type TaskIn struct {
	Tenant, Owner, ClientID, Tool, Key, ArgsDigest string
	ClientPolicy, Scenario                         string
}

// Store fakes workitems.Store plus the two additive task tables.
type Store struct {
	mu     sync.Mutex
	Design string // "A" or "B"
	// CrashAt k>0 crashes BEFORE step k; k == number of steps+1 crashes after
	// the final commit (the client never sees the response).
	CrashAt   int
	FailReads bool
	Now       func() time.Time
	NewTaskID func() string

	WIs    map[string]bool
	XRs    map[string]*XR
	Tasks  map[string]*TaskRow
	Scopes map[string]*Scope
	seq    int
}

// NewStore returns an empty store for the given design.
func NewStore(design string, now func() time.Time) *Store {
	return &Store{Design: design, Now: now, NewTaskID: RandomTaskID,
		WIs: map[string]bool{}, XRs: map[string]*XR{},
		Tasks: map[string]*TaskRow{}, Scopes: map[string]*Scope{}}
}

// RandomTaskID is "tk_" + base32(16 CSPRNG bytes).
func RandomTaskID() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return "tk_" + strings.ToLower(base32.StdEncoding.WithPadding(base32.NoPadding).EncodeToString(b))
}

func scopeKey(in TaskIn) string {
	return in.Tenant + "|" + in.Owner + "|" + in.Tool + "|" + in.Key
}

func (s *Store) crash(step int) error {
	if s.CrashAt == step {
		return fmt.Errorf("%w before step %d", ErrCrash, step)
	}
	return nil
}

// CreateTask creates or replays a task binding. Returns (taskID, replayed).
func (s *Store) CreateTask(in TaskIn) (string, bool, error) {
	s.mu.Lock() // stands for the scope advisory lock (design A step 1)
	defer s.mu.Unlock()
	if s.Design == "B" {
		return s.createB(in)
	}
	return s.createA(in)
}

// createA: one transaction. Staged writes become visible only at commit
// (step 7); a crash at any earlier step leaves nothing behind.
func (s *Store) createA(in TaskIn) (string, bool, error) {
	if err := s.crash(1); err != nil { // lock 1: scope
		return "", false, err
	}
	if err := s.crash(2); err != nil { // scope lookup
		return "", false, err
	}
	if sc, ok := s.Scopes[scopeKey(in)]; ok {
		if sc.ArgsDigest != in.ArgsDigest {
			return "", false, ErrKeyReused
		}
		return sc.TaskID, true, nil
	}
	taskID := s.NewTaskID()
	if err := s.crash(3); err != nil { // create or join wi_
		return "", false, err
	}
	s.seq++
	wi := fmt.Sprintf("wi_%d", s.seq)
	if err := s.crash(4); err != nil { // lock 2: work item
		return "", false, err
	}
	if err := s.crash(5); err != nil { // create xr_
		return "", false, err
	}
	xr := &XR{ID: fmt.Sprintf("xr_%d", s.seq), WIID: wi, State: "pending", Scenario: in.Scenario, TaskKey: taskID}
	if err := s.crash(6); err != nil { // insert binding
		return "", false, err
	}
	if err := s.crash(7); err != nil { // COMMIT
		return "", false, err
	}
	s.WIs[wi] = true
	s.XRs[xr.ID] = xr
	s.insertBinding(in, taskID, wi, xr.ID, "bound")
	if err := s.crash(8); err != nil { // after commit, before the wire
		return "", false, err
	}
	return taskID, false, nil
}

func (s *Store) insertBinding(in TaskIn, taskID, wi, xr, state string) {
	now := s.Now()
	s.Scopes[scopeKey(in)] = &Scope{TaskID: taskID, ArgsDigest: in.ArgsDigest, State: state, WIID: wi, XRID: xr,
		Tenant: in.Tenant, Owner: in.Owner, ClientID: in.ClientID, Tool: in.Tool, Key: in.Key,
		ClientPolicy: in.ClientPolicy, Scenario: in.Scenario, CreatedAt: now}
	if state == "bound" {
		s.bind(s.Scopes[scopeKey(in)])
	}
}

func (s *Store) bind(sc *Scope) {
	s.Tasks[sc.TaskID] = &TaskRow{TaskID: sc.TaskID, Tenant: sc.Tenant, Owner: sc.Owner, ClientID: sc.ClientID,
		Tool: sc.Tool, ArgsDigest: sc.ArgsDigest, Key: sc.Key, WIID: sc.WIID, XRID: sc.XRID,
		ClientPolicy: sc.ClientPolicy, CreatedAt: sc.CreatedAt}
}

// createB: durable intent first, then replayable steps, each its own commit.
func (s *Store) createB(in TaskIn) (string, bool, error) {
	sc, ok := s.Scopes[scopeKey(in)]
	replayed := ok
	if ok && sc.ArgsDigest != in.ArgsDigest {
		return "", false, ErrKeyReused
	}
	if err := s.crash(1); err != nil { // reserve
		return "", false, err
	}
	if !ok {
		s.insertBinding(in, s.NewTaskID(), "", "", "reserving")
		sc = s.Scopes[scopeKey(in)]
	}
	if err := s.resume(sc, 2); err != nil {
		return "", replayed, err
	}
	return sc.TaskID, replayed, nil
}

// resume runs steps 2..5 of design B idempotently.
func (s *Store) resume(sc *Scope, from int) error {
	if sc.State == "bound" {
		return nil
	}
	if err := s.crash(from); err != nil { // create wi_ (public, replayable)
		return err
	}
	if sc.WIID == "" {
		s.seq++
		sc.WIID = fmt.Sprintf("wi_%d", s.seq)
		s.WIs[sc.WIID] = true
	}
	if err := s.crash(from + 1); err != nil { // create xr_ (public, replayable)
		return err
	}
	if sc.XRID == "" {
		s.seq++
		sc.XRID = fmt.Sprintf("xr_%d", s.seq)
		s.XRs[sc.XRID] = &XR{ID: sc.XRID, WIID: sc.WIID, State: "pending", Scenario: sc.Scenario, TaskKey: sc.TaskID}
	}
	if err := s.crash(from + 2); err != nil { // mark bound
		return err
	}
	sc.State = "bound"
	s.bind(sc)
	return s.crash(from + 3) // after the last commit
}

// Sweep binds reserving rows older than bound (design B recovery).
func (s *Store) Sweep(bound time.Duration) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := 0
	for _, sc := range s.Scopes {
		if sc.State == "reserving" && s.Now().Sub(sc.CreatedAt) >= bound {
			saved := s.CrashAt
			s.CrashAt = 0
			_ = s.resume(sc, 2)
			s.CrashAt = saved
			n++
		}
	}
	return n
}

// PendingXRs is the dispatcher's claim scan.
func (s *Store) PendingXRs() []*XR {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []*XR
	for _, x := range s.XRs {
		if x.State == "pending" {
			out = append(out, x)
		}
	}
	return out
}

// OrphanPending returns pending xr_ rows with no bound task row (design A
// must never show any; design B shows them only under a reserving scope).
func (s *Store) OrphanPending() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []string
	for _, x := range s.XRs {
		if x.State != "pending" {
			continue
		}
		if t, ok := s.Tasks[x.TaskKey]; !ok || t.XRID != x.ID {
			out = append(out, x.ID)
		}
	}
	return out
}

// Task returns a binding or false.
func (s *Store) Task(id string) (*TaskRow, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.FailReads {
		return nil, false, ErrStore
	}
	t, ok := s.Tasks[id]
	return t, ok, nil
}

// XR reads an execution request.
func (s *Store) XR(id string) (*XR, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.FailReads {
		return nil, ErrStore
	}
	return s.XRs[id], nil
}

// RejectPending rejects a still-pending xr_ under the item lock.
func (s *Store) RejectPending(id, reason string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if x := s.XRs[id]; x != nil && x.State == "pending" {
		x.State, x.Reason = "rejected", reason
		return true
	}
	return false
}

// Claim is the dispatcher: pending xr_ -> claimed, with an execution id.
func (s *Store) Claim(id, weID string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if x := s.XRs[id]; x != nil && x.State == "pending" {
		x.State, x.WEID = "claimed", weID
		return true
	}
	return false
}

// Update runs fn on a task row under the store lock.
func (s *Store) Update(id string, fn func(*TaskRow)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if t := s.Tasks[id]; t != nil {
		fn(t)
	}
}

// GC deletes expired task rows and scope rows past their retention.
func (s *Store) GC() {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.Now()
	for id, t := range s.Tasks {
		if t.TerminalObservedAt != nil && !now.Before(t.TerminalObservedAt.Add(TerminalRetention)) {
			delete(s.Tasks, id)
			for _, sc := range s.Scopes {
				if sc.TaskID == id {
					exp := t.TerminalObservedAt.Add(TerminalRetention + XRReplayWindow)
					sc.ExpiresAt = &exp
				}
			}
		}
	}
	for k, sc := range s.Scopes {
		if sc.ExpiresAt != nil && !now.Before(*sc.ExpiresAt) {
			delete(s.Scopes, k)
		}
	}
}
