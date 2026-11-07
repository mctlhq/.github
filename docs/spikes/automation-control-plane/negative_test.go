package automationcp

import (
	"encoding/json"
	"fmt"
	"testing"
	"time"

	"mctl.ai/spikes/automationcp/model"
)

func evTrigger() model.Trigger {
	return model.Trigger{Kind: model.TrigEvent, EventType: "github.pull_request.opened", SubjectPrefix: "mctlhq/", FanoutCap: 3}
}

func (e *env) ingestID(id string) error {
	env := model.Envelope{SpecVersion: "mctl.events/v1", ID: id, Type: "github.pull_request.opened", Source: "mctl-github-ingress",
		OccurredAt: "2026-11-07T00:00:00Z", CorrelationID: "c-" + id, Subject: map[string]string{"kind": "github.pull_request", "repo": "mctlhq/demo"}}
	body, _ := json.Marshal(env)
	_, err := e.Svc.Ingest(e.Clock.Now(), model.Sign([]byte(e.w.HMACKey), e.Clock.Now(), body), body, "acme")
	return err
}

func TestForgedEventRejected(t *testing.T) { // T6
	e := newEnv(t)
	d := e.create("k", evTrigger())
	ts, _, body := e.event("pr_opened.json")
	_, err := e.Svc.Ingest(ts, "sha256=deadbeef", body, "acme")
	isErr(t, err, model.ErrSignatureInvalid)
	_, err = e.Svc.Ingest(ts, model.Sign([]byte("wrong-key"), ts, body), body, "acme")
	isErr(t, err, model.ErrSignatureInvalid)
	if n := len(e.runs(d.ID)); n != 0 || e.S.Ledger.Reserves != 0 {
		t.Fatalf("forged event started work: runs=%d", n)
	}
}

func TestReplayAndDuplicateDeduped(t *testing.T) { // T7
	e := newEnv(t)
	d := e.create("k", evTrigger())
	_, _, body := e.event("pr_opened.json")
	old := e.Clock.Now().Add(-10 * time.Minute)
	_, err := e.Svc.Ingest(old, model.Sign([]byte(e.w.HMACKey), old, body), body, "acme")
	isErr(t, err, model.ErrReplayWindow)
	ts, sig, _ := e.event("pr_opened.json")
	if _, err := e.Svc.Ingest(ts, sig, body, "acme"); err != nil {
		t.Fatal(err)
	}
	_, err = e.Svc.Ingest(ts, sig, body, "acme")
	isErr(t, err, model.ErrDuplicate)
	if len(e.runs(d.ID)) != 1 || e.S.Ledger.Reserves != 1 {
		t.Fatalf("runs=%d reserves=%d", len(e.runs(d.ID)), e.S.Ledger.Reserves)
	}
}

func TestBurstOverlapCapped(t *testing.T) { // T8
	e := newEnv(t)
	e.Svc.AutoRun = false
	d := e.create("k", evTrigger()) // fanout cap 3
	capped := 0
	for i := 0; i < 10; i++ {
		if err := e.ingestID(fmt.Sprintf("burst-%d", i)); err == model.ErrFanoutCapped {
			capped++
		} else if err != nil {
			t.Fatal(err)
		}
	}
	if n := len(e.runs(d.ID)); n != 3 || capped != 7 {
		t.Fatalf("runs=%d capped=%d, want 3 and 7", n, capped)
	}
}

func TestCrossTenantInspectAndCreateRefused(t *testing.T) { // T9
	e := newEnv(t)
	d := e.create("k", model.Trigger{Kind: model.TrigAt, At: e.Clock.Now().Add(time.Hour)})
	u2 := model.Caller{Principal: "u2", Surface: "claude"}
	if _, err := e.Svc.Get(u2, d.ID); err != model.ErrNotFound {
		t.Fatalf("get: %v", err)
	}
	if _, err := e.Svc.List(u2, "acme"); err != model.ErrNotFound {
		t.Fatalf("list: %v", err)
	}
	_, err := e.Svc.Create(u2, model.CreateRequest{IdempotencyKey: "x", Tenant: "acme", Owner: "u2", Template: "reminder.v1", Trigger: d.Trigger})
	isErr(t, err, model.ErrNotFound)
	if _, err := e.Svc.Get(u2, "au_9999"); err != model.ErrNotFound { // same answer as a missing id: no existence oracle
		t.Fatal("existence oracle")
	}
}

func TestRevokedInstallFencedAtFire(t *testing.T) { // T10
	e := newEnv(t)
	d := e.create("k", model.Trigger{Kind: model.TrigSchedule, Weekday: time.Saturday, Hour: 0, TZ: "UTC"})
	e.S.Authority.Revoke("inst-acme-1")
	for i := 0; i < 3; i++ {
		_, err := e.Svc.Fire(d.ID, fmt.Sprint("o", i))
		isErr(t, err, model.ErrInstallRevoked)
	}
	if d.State != model.StatePaused || e.Svc.AgentCalls != 0 || e.S.Ledger.Reserves != 0 {
		t.Fatalf("state=%s agent=%d reserves=%d", d.State, e.Svc.AgentCalls, e.S.Ledger.Reserves)
	}
}

func TestBudgetExceededRefused(t *testing.T) { // T11
	e := newEnv(t)
	d := e.create("k", model.Trigger{Kind: model.TrigAt, At: e.Clock.Now().Add(time.Hour)})
	d.Budget.PerRunMinor = 5 // run costs 10
	_, err := e.Svc.Fire(d.ID, "o1")
	isErr(t, err, model.ErrBudgetExceeded)
	d.Budget = model.Budget{PerRunMinor: 50, PerOwnerDayMinor: 15} // owner ceiling
	if _, err := e.Svc.Fire(d.ID, "o2"); err != nil {
		t.Fatal(err)
	}
	_, err = e.Svc.Fire(d.ID, "o3")
	isErr(t, err, model.ErrBudgetExceeded)
	if e.Svc.AgentCalls != 1 {
		t.Fatalf("agent called %d times", e.Svc.AgentCalls)
	}
}

func TestWorkerCrashRecovers(t *testing.T) { // T12
	e := newEnv(t)
	d := e.create("k", model.Trigger{Kind: model.TrigAt, At: e.Clock.Now().Add(time.Hour)})
	e.Svc.CrashAfter["reserved"] = true
	run, err := e.Svc.Fire(d.ID, "o1")
	isErr(t, err, model.ErrCrash)
	e.restart()
	if err := e.Svc.Advance(run.ID); err != nil {
		t.Fatal(err)
	}
	if run.Phase != "done" || e.S.Ledger.Reserves != 1 || len(e.runs(d.ID)) != 1 || !e.S.Ledger.Settled(run.ID) {
		t.Fatalf("phase=%s reserves=%d", run.Phase, e.S.Ledger.Reserves)
	}
}

func TestDeliveryFailureRecorded(t *testing.T) { // T13
	e := newEnv(t)
	e.Fail = true
	d := e.create("k", model.Trigger{Kind: model.TrigAt, At: e.Clock.Now().Add(time.Hour)})
	if _, err := e.Svc.Fire(d.ID, "o1"); err != nil {
		t.Fatal(err)
	}
	rs := e.runs(d.ID)
	if len(rs) != 1 || rs[0].Delivered != "failed" || rs[0].Phase != "done" {
		t.Fatalf("%+v", rs)
	}
	if e.S.ByID[rs[0].Execution].Delivery.Attempts != 3 {
		t.Fatal("retries not bounded at 3")
	}
}

func TestRawTemporalWorkflowRefused(t *testing.T) { // T14
	e := newEnv(t)
	for name, mut := range map[string]func(*model.CreateRequest){
		"workflow_type": func(r *model.CreateRequest) { r.WorkflowType = "DevLoopWorkflow" },
		"task_queue":    func(r *model.CreateRequest) { r.TaskQueue = "mctl-dev-loop" },
		"namespace":     func(r *model.CreateRequest) { r.Namespace = "mctl-agents" },
		"signal":        func(r *model.CreateRequest) { r.Signal = "approve" },
	} {
		r := model.CreateRequest{IdempotencyKey: name, Tenant: "acme", Owner: "u1", Template: "reminder.v1"}
		mut(&r)
		_, err := e.Svc.Create(e.U1, r)
		if err != model.ErrRawTemporal {
			t.Fatalf("%s: %v", name, err)
		}
	}
	if len(e.S.Defs) != 0 || len(e.S.Temporal.List()) != 0 {
		t.Fatal("refused request left state behind")
	}
}

func TestDeleteDoesNotTerminateInFlight(t *testing.T) { // T15
	for _, cancel := range []bool{false, true} {
		e := newEnv(t)
		e.Svc.AutoRun = false
		d := e.create("k", model.Trigger{Kind: model.TrigAt, At: e.Clock.Now().Add(time.Hour)})
		run, _ := e.Svc.Fire(d.ID, "o1")
		if _, err := e.Svc.Delete(e.U1, d.ID, d.Version, cancel); err != nil {
			t.Fatal(err)
		}
		if _, err := e.Svc.Fire(d.ID, "o2"); err != model.ErrNotFound {
			t.Fatalf("future fire after delete: %v", err)
		}
		if _, ok := e.S.Temporal.Get(model.ScheduleID(d.ID)); ok {
			t.Fatal("schedule survived delete")
		}
		_ = e.Svc.Advance(run.ID)
		want := "done"
		if cancel {
			want = "cancelled"
		}
		if run.Phase != want {
			t.Fatalf("cancel=%v phase=%s want %s", cancel, run.Phase, want)
		}
	}
}

func TestIdempotentCreateAndOptimisticUpdate(t *testing.T) { // T16
	e := newEnv(t)
	tr := model.Trigger{Kind: model.TrigAt, At: e.Clock.Now().Add(time.Hour)}
	a := e.create("same", tr)
	b := e.create("same", tr)
	if a.ID != b.ID || len(e.S.Defs) != 1 || e.S.Temporal.Creates != 1 {
		t.Fatal("idempotent create made a second automation")
	}
	_, err := e.Svc.Create(e.U1, model.CreateRequest{IdempotencyKey: "same", Tenant: "acme", Owner: "u1", Template: "agent.investigate-ci-failure.v1", Trigger: tr})
	isErr(t, err, model.ErrIdempotencyReused)
	if _, err := e.Svc.Pause(e.U1, a.ID, 99); err != model.ErrVersionConflict {
		t.Fatalf("stale version: %v", err)
	}
	p, err := e.Svc.Pause(e.U1, a.ID, a.Version)
	if err != nil || p.State != model.StatePaused || p.Version != 2 {
		t.Fatalf("pause: %v %+v", err, p)
	}
	if _, err := e.Svc.Resume(e.U1, a.ID, 1); err != model.ErrVersionConflict {
		t.Fatal("stale resume accepted")
	}
}

func TestOverlappingFiresUseDistinctWorkItems(t *testing.T) { // T18
	e := newEnv(t)
	e.Svc.AutoRun = false
	d := e.create("k", model.Trigger{Kind: model.TrigSchedule, Weekday: time.Friday, Hour: 9, TZ: "UTC", Overlap: model.OverlapAllowAll})
	a, _ := e.Svc.Fire(d.ID, "occ-1")
	b, _ := e.Svc.Fire(d.ID, "occ-2")
	if a.WorkItem == b.WorkItem || a.ID == b.ID {
		t.Fatal("two simultaneous fires shared a wi_ or we_")
	}
	a2, _ := e.Svc.Fire(d.ID, "occ-1") // replay of one occurrence
	if a2 != a || len(e.runs(d.ID)) != 2 || e.S.Ledger.Reserves != 2 {
		t.Fatalf("replay changed state: runs=%d reserves=%d", len(e.runs(d.ID)), e.S.Ledger.Reserves)
	}
	nonTerminal := map[string]int{}
	for _, r := range e.runs(d.ID) {
		nonTerminal[r.WorkItem]++
	}
	for wi, n := range nonTerminal {
		if n > 1 {
			t.Fatalf("%s has %d open executions", wi, n)
		}
	}
	// A retry of an occurrence whose we_ is still open must not start a second we_.
	w, _ := e.S.EnsureWorkItem(d.ID, "occ-1")
	if again, fresh := e.S.StartExecution(w); fresh || again != a {
		t.Fatal("second non-terminal we_ on one wi_")
	}
}

func TestScheduleCreateCrashReconciles(t *testing.T) { // T19
	e := newEnv(t)
	e.Svc.CrashAfterTemporalCreate = true
	at := e.Clock.Now().Add(time.Hour)
	d, err := e.Svc.Create(e.U1, model.CreateRequest{IdempotencyKey: "k", Tenant: "acme", Owner: "u1", Template: "reminder.v1",
		Trigger: model.Trigger{Kind: model.TrigAt, At: at}, Install: "inst-acme-1", InstallRev: 1, Budget: model.Budget{PerRunMinor: 50, PerOwnerDayMinor: 500}})
	isErr(t, err, model.ErrCrash)
	if e.S.Desired[d.ID].Applied || e.S.Temporal.Creates != 1 {
		t.Fatal("precondition: Temporal accepted, MCTL did not record success")
	}
	// An unobservable Temporal must not be read as "absent": no orphan deletion, repair still retried.
	e.S.Temporal.Scheds["sch-au_orphan"] = &model.Sched{ID: "sch-au_orphan", Automation: "au_orphan"}
	if _, orphans := e.Svc.Reconcile(false); orphans != 0 {
		t.Fatal("orphan deleted on incomplete observation")
	}
	if _, orphans := e.Svc.Reconcile(true); orphans != 1 {
		t.Fatalf("orphans=%d", orphans)
	}
	if !e.S.Desired[d.ID].Applied || e.S.Temporal.Creates != 1 || len(e.S.Temporal.List()) != 1 {
		t.Fatalf("not converged: creates=%d scheds=%d", e.S.Temporal.Creates, len(e.S.Temporal.List()))
	}
	e.Clock.Set(at.Add(time.Second))
	e.Svc.Tick()
	e.Svc.Tick()
	if n := len(e.runs(d.ID)); n != 1 {
		t.Fatalf("fires=%d after repair, want exactly 1", n)
	}
	// Unauthorized desired state never executes.
	e2 := newEnv(t)
	d2 := e2.create("k", model.Trigger{Kind: model.TrigAt, At: e2.Clock.Now().Add(time.Hour)})
	e2.S.Authority.RemoveMember("acme", "u1")
	e2.Clock.Advance(2 * time.Hour)
	e2.Svc.Tick()
	if e2.Svc.AgentCalls != 0 || e2.S.Ledger.Reserves != 0 || len(e2.runs(d2.ID)) != 1 || e2.runs(d2.ID)[0].Phase != "refused" {
		t.Fatalf("unauthorized desired state executed: %+v", e2.runs(d2.ID))
	}
}

func TestRevocationBeforeSideEffect(t *testing.T) { // T20
	e := newEnv(t)
	d := e.create("k", model.Trigger{Kind: model.TrigAt, At: e.Clock.Now().Add(time.Hour)})
	e.Svc.BeforeAction = func() { e.S.Authority.Revoke("inst-acme-1") } // after the fire-time checkpoint passed
	run, err := e.Svc.Fire(d.ID, "o1")
	isErr(t, err, model.ErrInstallRevoked)
	if run.Phase != "refused" || len(e.Sent) != 0 || e.Svc.AgentCalls != 1 {
		t.Fatalf("side effect leaked: phase=%s sent=%v", run.Phase, e.Sent)
	}
}

func TestUnknownCrossSurfaceIdentityRefused(t *testing.T) { // T21
	e := newEnv(t)
	d := e.create("k", model.Trigger{Kind: model.TrigAt, At: e.Clock.Now().Add(time.Hour)})
	e.S.Authority.Unlink("u1", "telegram")
	tg := model.Caller{Principal: "u1", Surface: "telegram"}
	if _, err := e.Svc.Get(tg, d.ID); err != model.ErrIdentityUnlinked {
		t.Fatalf("get: %v", err)
	}
	if _, err := e.Svc.Pause(tg, d.ID, d.Version); err != model.ErrIdentityUnlinked {
		t.Fatalf("pause: %v", err)
	}
	// The same canonical principal on a linked surface still works: same automation_id.
	if got, err := e.Svc.Get(model.Caller{Principal: "u1", Surface: "chatgpt"}, d.ID); err != nil || got.ID != d.ID {
		t.Fatalf("linked surface: %v", err)
	}
	if d.State != model.StateEnabled {
		t.Fatal("unlinked surface mutated the automation")
	}
}

func TestUnsupportedCiEventIsNotAssumedAvailable(t *testing.T) { // T22
	e := newEnv(t)
	d := e.create("k", model.Trigger{Kind: model.TrigEvent, EventType: "github.check_run.completed", SubjectPrefix: "mctlhq/", FanoutCap: 3})
	ts, sig, body := e.event("check_run_failed.json")
	_, err := e.Svc.Ingest(ts, sig, body, "acme")
	isErr(t, err, model.ErrUnsupportedEvent)
	if len(e.runs(d.ID)) != 0 {
		t.Fatal("unsupported CI event started a run")
	}
	ts, sig, body = e.event("pr_opened.json") // supported now
	e.create("k2", evTrigger())
	if _, err := e.Svc.Ingest(ts, sig, body, "acme"); err != nil {
		t.Fatal(err)
	}
}
