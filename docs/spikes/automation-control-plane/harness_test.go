package automationcp

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"mctl.ai/spikes/automationcp/model"
)

type world struct {
	HMACKey string              `json:"hmac_key"`
	Tenants map[string][]string `json:"tenants"`
	Links   map[string][]string `json:"links"`
	Support struct {
		Principal   string `json:"principal"`
		BoundTenant string `json:"bound_tenant"`
	} `json:"support"`
	Templates []model.Template `json:"templates"`
}

type env struct {
	t     *testing.T
	w     world
	S     *model.Stores
	Clock *model.FakeClock
	Svc   *model.Service
	Sent  []string
	Fail  bool
	U1    model.Caller
}

func (e *env) Send(chat, text string) error {
	if e.Fail {
		return os.ErrDeadlineExceeded
	}
	e.Sent = append(e.Sent, chat)
	return nil
}

func readJSON(t testing.TB, path string, v any) {
	b, err := os.ReadFile(filepath.Join("fixtures", path))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(b, v); err != nil {
		t.Fatal(err)
	}
}

func newEnv(t *testing.T) *env {
	e := &env{t: t}
	readJSON(t, "world.json", &e.w)
	e.S = model.NewStores()
	e.Clock = model.NewFakeClock(time.Date(2026, 11, 7, 0, 0, 0, 0, time.UTC))
	tpl := map[string]model.Template{}
	for _, x := range e.w.Templates {
		tpl[x.Name] = x
	}
	for tenant, ms := range e.w.Tenants {
		for _, m := range ms {
			e.S.Authority.AddMember(tenant, m)
		}
	}
	for p, ss := range e.w.Links {
		for _, s := range ss {
			e.S.Authority.Link(p, s)
		}
	}
	e.Svc = model.NewService(e.S, e.Clock, tpl)
	e.Svc.Telegram = e
	e.Svc.Verifier = &model.Verifier{Key: []byte(e.w.HMACKey), Window: 5 * time.Minute, Clock: e.Clock}
	e.U1 = model.Caller{Principal: "u1", Surface: "claude"}
	return e
}

func (e *env) restart() { // a worker/API restart: same durable stores, fresh service
	old := e.Svc
	e.Svc = model.NewService(e.S, e.Clock, old.Templates)
	e.Svc.Telegram, e.Svc.Verifier = e, old.Verifier
}

func (e *env) create(key string, tr model.Trigger) *model.Definition {
	e.t.Helper()
	d, err := e.Svc.Create(e.U1, model.CreateRequest{IdempotencyKey: key, Tenant: "acme", Owner: "u1", Template: "reminder.v1",
		Trigger: tr, Install: "inst-acme-1", InstallRev: 1, Budget: model.Budget{PerRunMinor: 50, PerOwnerDayMinor: 500}, DeliveryChat: "chat-1"})
	if err != nil {
		e.t.Fatalf("create: %v", err)
	}
	return d
}

func (e *env) event(file string) (time.Time, string, []byte) {
	b, err := os.ReadFile(filepath.Join("fixtures", "events", file))
	if err != nil {
		e.t.Fatal(err)
	}
	ts := e.Clock.Now()
	return ts, model.Sign([]byte(e.w.HMACKey), ts, b), b
}

func (e *env) runs(au string) []model.Run { return e.S.Runs(au) }

func berlin(t *testing.T) *time.Location {
	l, err := time.LoadLocation("Europe/Berlin")
	if err != nil {
		t.Fatal(err)
	}
	return l
}

func isErr(t *testing.T, got, want error) {
	t.Helper()
	if got != want {
		t.Fatalf("got %v, want %v", got, want)
	}
}

func TestAtOnceFiresExactlyOnce(t *testing.T) { // T1
	e := newEnv(t)
	at := time.Date(2026, 11, 8, 9, 0, 0, 0, berlin(t))
	d := e.create("k1", model.Trigger{Kind: model.TrigAt, At: at})
	e.Clock.Set(at.Add(-time.Minute))
	e.Svc.Tick()
	if n := len(e.runs(d.ID)); n != 0 {
		t.Fatalf("fired early: %d", n)
	}
	e.Clock.Set(at.Add(time.Second))
	e.Svc.Tick()
	e.restart()
	e.Clock.Advance(time.Hour)
	e.Svc.Tick()
	e.Svc.Tick()
	if rs := e.runs(d.ID); len(rs) != 1 || rs[0].Phase != "done" || rs[0].Delivered != "delivered" {
		t.Fatalf("runs=%+v", rs)
	}
}

func TestAtDSTGapAndOverlap(t *testing.T) { // T2: explicit named policies, not assumed
	loc := berlin(t)
	// Gap 2027-03-28 02:30 Berlin does not exist. Observed Temporal: skipped. Product option: shift_forward.
	if _, ok := model.ResolveLocal(2027, 3, 28, 2, 30, loc, "skip", "later"); ok {
		t.Fatal("skip policy must not fire in a gap")
	}
	sf, ok := model.ResolveLocal(2027, 3, 28, 2, 30, loc, "shift_forward", "later")
	if !ok || sf.UTC().Format(time.RFC3339) != "2027-03-28T01:30:00Z" {
		t.Fatalf("shift_forward: %v %v", sf, ok)
	}
	// Overlap 2027-10-31 02:30 occurs twice. Observed Temporal: once, the later (CET) instant.
	later, _ := model.ResolveLocal(2027, 10, 31, 2, 30, loc, "skip", "later")
	first, _ := model.ResolveLocal(2027, 10, 31, 2, 30, loc, "skip", "first")
	if later.UTC().Format(time.RFC3339) != "2027-10-31T01:30:00Z" || first.UTC().Format(time.RFC3339) != "2027-10-31T00:30:00Z" {
		t.Fatalf("overlap later=%v first=%v", later, first)
	}
	// End to end: a local-wall `at` in the gap fires nothing under skip, once under shift_forward.
	for pol, want := range map[string]int{"skip": 0, "shift_forward": 1} {
		e := newEnv(t)
		e.Clock.Set(time.Date(2027, 3, 27, 0, 0, 0, 0, time.UTC))
		d := e.create("k-"+pol, model.Trigger{Kind: model.TrigAt, LocalWall: "2027-03-28T02:30", TZ: "Europe/Berlin", GapPolicy: pol})
		e.Clock.Set(time.Date(2027, 3, 28, 3, 0, 0, 0, time.UTC))
		e.Svc.Tick()
		if n := len(e.runs(d.ID)); n != want {
			t.Fatalf("%s: %d runs, want %d", pol, n, want)
		}
	}
}

func TestScheduleRecurrenceOverlapSkip(t *testing.T) { // T3a
	e := newEnv(t)
	e.Svc.AutoRun = false // runs stay open so the next fire overlaps
	loc := berlin(t)
	d := e.create("k", model.Trigger{Kind: model.TrigSchedule, Weekday: time.Friday, Hour: 9, Minute: 0, TZ: "Europe/Berlin", Overlap: model.OverlapSkip})
	fri := time.Date(2026, 11, 13, 9, 0, 0, 0, loc)
	e.Clock.Set(fri.Add(time.Second))
	e.Svc.Tick()
	if rs := e.runs(d.ID); len(rs) != 1 {
		t.Fatalf("first fire: %+v", rs)
	}
	e.Clock.Set(fri.AddDate(0, 0, 7).Add(time.Second))
	e.Svc.Tick() // previous run still open -> skipped
	if rs := e.runs(d.ID); len(rs) != 1 {
		t.Fatalf("overlap skip failed: %+v", rs)
	}
	if s, _ := e.S.Temporal.Get(model.ScheduleID(d.ID)); s.Skipped != 1 {
		t.Fatalf("skipped=%d", s.Skipped)
	}
}

func TestScheduleCatchupWindow(t *testing.T) { // T3b
	for _, tc := range []struct {
		name string
		late time.Duration
		want int
	}{{"inside window runs once", 30 * time.Minute, 1}, {"outside window dropped", 3 * time.Hour, 0}} {
		e := newEnv(t)
		loc := berlin(t)
		d := e.create("k", model.Trigger{Kind: model.TrigSchedule, Weekday: time.Friday, Hour: 9, TZ: "Europe/Berlin", CatchUp: time.Hour})
		fri := time.Date(2026, 11, 13, 9, 0, 0, 0, loc)
		e.Clock.Set(fri.Add(tc.late)) // scheduler was down; first tick after the outage
		e.Svc.Tick()
		if n := len(e.runs(d.ID)); n != tc.want {
			t.Fatalf("%s: %d runs, want %d", tc.name, n, tc.want)
		}
	}
}

func TestSignedWebhookStartsRun(t *testing.T) { // T4a
	e := newEnv(t)
	d := e.create("k", model.Trigger{Kind: model.TrigEvent, EventType: "github.pull_request.opened", SubjectPrefix: "mctlhq/", FanoutCap: 5})
	ts, sig, body := e.event("pr_opened.json")
	dec, err := e.Svc.Ingest(ts, sig, body, "acme")
	if err != nil || dec != "start" || len(e.runs(d.ID)) != 1 {
		t.Fatalf("dec=%q err=%v runs=%v", dec, err, e.runs(d.ID))
	}
}

func TestDependencySignalsAwaitingRun(t *testing.T) { // T4b
	e := newEnv(t)
	d := e.create("k", model.Trigger{Kind: model.TrigDependency, EventType: "github.pull_request.opened", SubjectPrefix: "mctlhq/", Await: true})
	run, err := e.Svc.Run(e.U1, d.ID, "m1")
	if err != nil || run.Phase != "awaiting_signal" {
		t.Fatalf("run=%+v err=%v", run, err)
	}
	ts, sig, body := e.event("pr_opened.json")
	dec, err := e.Svc.Ingest(ts, sig, body, "acme")
	if err != nil || dec != "signal" || run.Signals != 1 || run.Phase != "done" || len(e.runs(d.ID)) != 1 {
		t.Fatalf("dec=%q err=%v run=%+v runs=%d", dec, err, run, len(e.runs(d.ID)))
	}
}

func TestSupportFollowupBounded(t *testing.T) { // T5
	e := newEnv(t)
	sup := model.Caller{Principal: "svc-support", Surface: "support", Actor: "support", BoundTenant: "acme"}
	req := model.CreateRequest{IdempotencyKey: "s1", Tenant: "acme", Owner: "u1", Template: "support.diagnostic-followup.v1",
		Trigger: model.Trigger{Kind: model.TrigSchedule, Weekday: e.Clock.Now().Weekday(), Hour: 0, TZ: "UTC"}, Install: "inst-acme-1", InstallRev: 1,
		Budget: model.Budget{PerRunMinor: 20, PerOwnerDayMinor: 100}, TTL: 24 * time.Hour, MaxFires: 3, AuthRef: "owner-auth-ref"}
	d, err := e.Svc.Create(sup, req)
	if err != nil || !d.ReadOnly {
		t.Fatalf("create: %v %+v", err, d)
	}
	for i := 0; i < 5; i++ {
		_, _ = e.Svc.Fire(d.ID, "o"+string(rune('a'+i)))
	}
	ok := 0
	for _, r := range e.runs(d.ID) {
		if r.Phase == "done" {
			ok++
		}
	}
	if ok != 3 {
		t.Fatalf("fires=%d, want 3 (cap)", ok)
	}
	e.Clock.Advance(25 * time.Hour)
	if _, err := e.Svc.Get(sup, d.ID); err != model.ErrNotFound {
		t.Fatalf("support read after TTL: %v", err)
	}
	for name, mut := range map[string]func(*model.CreateRequest){
		"mutating template": func(r *model.CreateRequest) { r.Template = "agent.deploy.v1" },
		"ttl over 24h":      func(r *model.CreateRequest) { r.TTL = 48 * time.Hour },
		"no auth ref":       func(r *model.CreateRequest) { r.AuthRef = "" },
	} {
		r := req
		r.IdempotencyKey = "bad-" + name
		mut(&r)
		if _, err := e.Svc.Create(sup, r); err != model.ErrTemplateForbidden {
			t.Fatalf("%s: %v", name, err)
		}
	}
	r := req
	r.IdempotencyKey, r.Tenant = "x", "globex"
	if _, err := e.Svc.Create(sup, r); err != model.ErrNotFound {
		t.Fatalf("cross-tenant support create: %v", err)
	}
	if !strings.Contains(req.Template, "support.") {
		t.Fatal("unreachable")
	}
}
