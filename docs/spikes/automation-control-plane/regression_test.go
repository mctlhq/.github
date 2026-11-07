package automationcp

import (
    "encoding/json"
    "os"
    "strings"
    "testing"
    "time"

    "mctl.ai/spikes/automationcp/model"
)

// A create input must never require or permit server-owned definition fields.
func TestCreateRequestSchemaSeparatesServerFields(t *testing.T) {
    body, err := os.ReadFile("schemas/automation-create.v0.schema.json")
    if err != nil { t.Fatal(err) }
    var schema struct {
        Required []string `json:"required"`
        Properties map[string]json.RawMessage `json:"properties"`
        AdditionalProperties bool `json:"additionalProperties"`
    }
    if err := json.Unmarshal(body, &schema); err != nil { t.Fatal(err) }
    if schema.AdditionalProperties { t.Fatal("create schema permits server-owned extra fields") }
    for _, field := range []string{"automation_id", "version", "lifecycle", "provenance", "last_run", "next_run_at", "creating_surface"} {
        if _, ok := schema.Properties[field]; ok { t.Fatalf("create input exposes server field %s", field) }
        for _, required := range schema.Required {
            if required == field { t.Fatalf("create input requires server field %s", field) }
        }
    }
    if _, ok := schema.Properties["auth_ref"]; !ok {
        t.Fatal("support follow-up create cannot carry a verified owner auth_ref")
    }
    if _, ok := schema.Properties["delegated_actor"]; ok {
        t.Fatal("delegated_actor must come from the authenticated server identity")
    }
    for _, required := range []string{"tenant", "owner", "template", "trigger", "delivery", "budget"} {
        found := false
        for _, field := range schema.Required { if field == required { found = true } }
        if !found { t.Fatalf("required client field %s is missing", required) }
    }
    opsBody, err := os.ReadFile("schemas/operations.v0.schema.json")
    if err != nil { t.Fatal(err) }
    var ops struct {
        OneOf []struct {
            Title string `json:"title"`
            Properties map[string]struct { Ref string `json:"$ref"` } `json:"properties"`
        } `json:"oneOf"`
    }
    if err := json.Unmarshal(opsBody, &ops); err != nil { t.Fatal(err) }
    for _, operation := range ops.OneOf {
        if operation.Title == "mctl_create_automation" {
            if operation.Properties["definition"].Ref != "automation-create.v0.schema.json" {
                t.Fatalf("create operation references the wrong definition: %s", operation.Properties["definition"].Ref)
            }
            return
        }
    }
    t.Fatal("mctl_create_automation schema missing")
}

func TestDependencyEventFanoutSignalsEverySubscriber(t *testing.T) {
    e := newEnv(t)
    trigger := model.Trigger{Kind: model.TrigDependency, EventType: "github.pull_request.opened", SubjectPrefix: "mctlhq/", Await: true}
    first := e.create("first", trigger)
    second := e.create("second", trigger)
    normal := e.create("normal", evTrigger())
    a, err := e.Svc.Run(e.U1, first.ID, "wait-1")
    if err != nil { t.Fatal(err) }
    b, err := e.Svc.Run(e.U1, second.ID, "wait-2")
    if err != nil { t.Fatal(err) }
    if a.Phase != "awaiting_signal" || b.Phase != "awaiting_signal" { t.Fatal("dependency run did not await") }
    ts, sig, body := e.event("pr_opened.json")
    decision, err := e.Svc.Ingest(ts, sig, body, "acme")
    if err != nil { t.Fatal(err) }
    if decision != "signal" || a.Signals != 1 || b.Signals != 1 || a.Phase != "done" || b.Phase != "done" {
        t.Fatalf("fanout dropped a waiting subscriber: decision=%s a=%+v b=%+v", decision, a, b)
    }
    if len(e.runs(normal.ID)) != 1 { t.Fatal("event subscriber following a signal was never triggered") }
    _, err = e.Svc.Ingest(ts, sig, body, "acme")
    if err != model.ErrDuplicate || a.Signals != 1 || b.Signals != 1 || len(e.runs(normal.ID)) != 1 {
        t.Fatalf("duplicate delivery changed subscriber state: %v", err)
    }
}

func TestBufferOneDrainsAfterPriorExecutionCompletes(t *testing.T) {
    e := newEnv(t)
    e.Svc.AutoRun = false
    tr := model.Trigger{Kind: model.TrigSchedule, Weekday: time.Friday, Hour: 9, TZ: "UTC", Overlap: model.OverlapBufferOne}
    d := e.create("buffer", tr)
    start := time.Date(2026, 11, 13, 9, 0, 0, 0, time.UTC)
    e.Clock.Set(start.Add(time.Second)); e.Svc.Tick()
    runs := e.runs(d.ID)
    if len(runs) != 1 { t.Fatalf("first fire: %+v", runs) }
    first := runs[0].Execution
    secondAt := start.AddDate(0, 0, 7)
    e.Clock.Set(secondAt.Add(time.Second)); e.Svc.Tick()
    thirdAt := start.AddDate(0, 0, 14)
    e.Clock.Set(thirdAt.Add(time.Second)); e.Svc.Tick()
    sched, _ := e.S.Temporal.Get(model.ScheduleID(d.ID))
    if sched.Buffered == nil || !sched.Buffered.Equal(secondAt) || len(e.runs(d.ID)) != 1 {
        t.Fatalf("should retain one buffered occurrence (the first): %+v", sched)
    }
    if err := e.Svc.Advance(first); err != nil { t.Fatal(err) }
    e.Clock.Advance(time.Second); e.Svc.Tick() // no new nominal fire, but drain the buffer
    runs = e.runs(d.ID)
    if len(runs) != 2 || sched.Buffered != nil { t.Fatalf("buffer was not dispatched: %+v", runs) }
    wantKey := d.ID + "|occ-" + secondAt.UTC().Format(time.RFC3339)
    if _, ok := e.S.WorkItems[wantKey]; !ok { t.Fatalf("buffered occurrence key %s not executed", wantKey) }
}

func TestCancelOtherReleasesUnsettledBudget(t *testing.T) {
    e := newEnv(t)
    e.Svc.AutoRun = false
    tr := model.Trigger{Kind: model.TrigSchedule, Weekday: time.Friday, Hour: 9, TZ: "UTC", Overlap: model.OverlapCancelOther}
    d := e.create("cancel", tr)
    d.Budget.PerOwnerDayMinor = 10 // only one reservation fits at any one time
    firstAt := time.Date(2026, 11, 13, 9, 0, 0, 0, time.UTC)
    e.Clock.Set(firstAt.Add(time.Second)); e.Svc.Tick()
    first := e.runs(d.ID)[0].Execution
    e.Clock.Set(firstAt.AddDate(0, 0, 7).Add(time.Second)); e.Svc.Tick()
    rs := e.runs(d.ID)
    if len(rs) != 2 || e.S.ByID[first].Phase != "cancelled" || e.S.Ledger.Reserves != 2 {
        t.Fatalf("cancel_other did not start replacement and release previous budget: %+v", rs)
    }
    for _, run := range rs {
        if run.Execution != first && run.Phase != "reserved" { t.Fatalf("replacement refused due to leaked budget: %+v", rs) }
    }
}

func TestTelegramEventUsesChatIDSubject(t *testing.T) {
    e := newEnv(t)
    d := e.create("telegram", model.Trigger{Kind: model.TrigEvent, EventType: "telegram.message.created", SubjectPrefix: "chat-1"})
    event := model.Envelope{SpecVersion: "mctl.events/v1", ID: "tg-msg-1", Type: "telegram.message.created",
        Source: "mctl-telegram", OccurredAt: e.Clock.Now().Format(time.RFC3339),
        CorrelationID: "tg-corr-1", Subject: map[string]string{"kind": "telegram.message", "chat_id": "chat-123"}}
    body, err := json.Marshal(event)
    if err != nil { t.Fatal(err) }
    ts := e.Clock.Now()
    decision, err := e.Svc.Ingest(ts, model.Sign([]byte(e.w.HMACKey), ts, body), body, "acme")
    if err != nil || decision != "start" || len(e.runs(d.ID)) != 1 {
        t.Fatalf("telegram chat_id routing: %s %v %+v", decision, err, e.runs(d.ID))
    }
}

func TestBudgetRefusalDoesNotExhaustMaxFires(t *testing.T) {
    e := newEnv(t)
    d := e.create("support-max", model.Trigger{Kind: model.TrigAt, At: e.Clock.Now().Add(time.Hour)})
    d.MaxFires = 3
    d.Budget.PerRunMinor = 5
    _, err := e.Svc.Fire(d.ID, "budget-rejected")
    if err != model.ErrBudgetExceeded || d.Fires != 0 {
        t.Fatalf("rejected reservation consumed MaxFires: %v %d", err, d.Fires)
    }
    d.Budget.PerRunMinor = 50
    for _, occurrence := range []string{"success-1", "success-2", "success-3"} {
        if _, err := e.Svc.Fire(d.ID, occurrence); err != nil { t.Fatal(err) }
    }
    if d.Fires != 3 {
        t.Fatalf("expected three successful reservations, got %d", d.Fires)
    }
    _, err = e.Svc.Fire(d.ID, "beyond-cap")
    if err != nil { t.Fatal(err) }
    if e.Svc.AgentCalls != 3 { t.Fatal("max fires exceeded") }
}

func TestAdvanceUnknownExecutionIsNotFound(t *testing.T) {
    e := newEnv(t)
    if err := e.Svc.Advance("we_nonexistent"); err != model.ErrNotFound {
        t.Fatalf("missing execution caused panic or wrong error: %v", err)
    }
    // A lookup is safe while another thread inserts definitions.
    done := make(chan struct{})
    go func() {
        defer close(done)
        for i := 0; i < 32; i++ {
            if err := e.Svc.Advance("we_nonexistent"); err != model.ErrNotFound {
                t.Errorf("concurrent unknown execution: %v", err)
            }
        }
    }()
    for i := 0; i < 32; i++ {
        // Distinct idempotency keys only; all writes go through the store mutex.
        if _, err := e.Svc.Create(e.U1, model.CreateRequest{IdempotencyKey: strings.Repeat("k", i+1), Tenant: "acme", Owner: "u1",
            Template: "reminder.v1", Trigger: model.Trigger{Kind: model.TrigEvent, EventType: "github.pull_request.opened"},
            Budget: model.Budget{PerRunMinor: 100, PerOwnerDayMinor: 1000}}); err != nil { t.Fatal(err) }
    }
    <-done
}
