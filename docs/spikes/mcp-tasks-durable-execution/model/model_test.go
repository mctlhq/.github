package model

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

type env struct {
	t   *testing.T
	now time.Time
	st  *Store
	tw  *Temporal
	ad  *Adapter
}

var human = Principal{ID: "u1", Tenant: "acme", ClientID: "c1", ActorRef: "alice", Class: "human"}

func newEnv(t *testing.T, design string) *env {
	e := &env{t: t, now: time.Date(2026, 11, 22, 12, 0, 0, 0, time.UTC)}
	clk := func() time.Time { return e.now }
	e.st = NewStore(design, clk)
	e.tw = NewTemporal(clk)
	revoked := map[string]bool{}
	e.tw.Revoked = revoked
	e.ad = &Adapter{Store: e.st, Temporal: e.tw, Now: clk, Revoked: revoked}
	return e
}

func meta(declare bool) map[string]any {
	if !declare {
		return map[string]any{}
	}
	return map[string]any{"io.modelcontextprotocol/clientCapabilities": map[string]any{
		"extensions": map[string]any{Extension: map[string]any{}}}}
}

func (e *env) create(p Principal, key, scenario string, declare bool) map[string]any {
	return e.ad.Handle(Request{Method: "tools/call", Principal: p, Params: map[string]any{
		"name": "probe", "_meta": meta(declare),
		"arguments": map[string]any{"idempotency_key": key, "scenario": scenario}}})
}

func (e *env) newTask(key, scenario string) string {
	r := e.create(human, key, scenario, true)["result"].(map[string]any)
	Dispatch(e.st, e.tw)
	return r["taskId"].(string)
}

func (e *env) call(method, id string, p Principal, extra map[string]any) map[string]any {
	params := map[string]any{"taskId": id, "_meta": meta(true)}
	for k, v := range extra {
		params[k] = v
	}
	return e.ad.Handle(Request{Method: method, Params: params, MCPName: id, Principal: p})
}

func (e *env) get(id string) map[string]any {
	r := e.call("tasks/get", id, human, nil)
	if r["error"] != nil {
		e.t.Fatalf("get error: %v", r["error"])
	}
	return r["result"].(map[string]any)
}

func code(r map[string]any) int {
	if m, ok := r["error"].(map[string]any); ok {
		return m["code"].(int)
	}
	return 0
}

func (e *env) answer(id, key, val string, p Principal) {
	e.call("tasks/update", id, p, map[string]any{"inputResponses": map[string]any{
		key: map[string]any{"action": "accept", "content": map[string]any{"value": val}}}})
}

func (e *env) reqID(id string) string {
	return e.tw.WFs[e.st.XRs[e.st.Tasks[id].XRID].WEID].Req.RequestID
}

func TestT1CreatePoll(t *testing.T) {
	e := newEnv(t, "A")
	r := e.create(human, "k1", "sleep", true)["result"].(map[string]any)
	if r["resultType"] != "task" {
		t.Fatal("create must return resultType task")
	}
	id := r["taskId"].(string)
	g := e.get(id) // immediately, before dispatch
	if g["resultType"] != "complete" || g["status"] != "working" {
		t.Fatalf("got %v", g)
	}
}

func TestT2NonNegotiating(t *testing.T) {
	e := newEnv(t, "A")
	r := e.create(human, "k1", "sleep", false)["result"].(map[string]any)
	sc := r["structuredContent"].(map[string]any)
	if sc["work_item_id"] == nil || sc["execution_request_id"] == nil {
		t.Fatal("missing wi_/xr_ references")
	}
	id := e.st.Scopes["acme|u1|probe|k1"].TaskID
	for _, m := range []string{"tasks/get", "tasks/update", "tasks/cancel"} {
		// extension declared on an earlier request only: still -32021
		got := e.ad.Handle(Request{Method: m, MCPName: id, Principal: human, Params: map[string]any{"taskId": id}})
		if code(got) != CodeMissingCap {
			t.Fatalf("%s: %v", m, got)
		}
	}
}

func TestT3T5T6Idempotency(t *testing.T) {
	e := newEnv(t, "A")
	a := e.create(human, "k1", "sleep", true)["result"].(map[string]any)["taskId"]
	b := e.create(human, "k1", "sleep", true)["result"].(map[string]any)["taskId"]
	if a != b || len(e.st.XRs) != 1 {
		t.Fatalf("duplicate create: %v %v xrs=%d", a, b, len(e.st.XRs))
	}
	Dispatch(e.st, e.tw)
	if len(e.tw.WFs) != 1 {
		t.Fatal("one execution expected")
	}
	if code(e.create(human, "k1", "other", true)) != CodeInvalidParams {
		t.Fatal("key reuse must be refused")
	}
	if code(e.ad.Handle(Request{Method: "tools/call", Principal: human,
		Params: map[string]any{"_meta": meta(true), "arguments": map[string]any{}}})) != CodeInvalidParams {
		t.Fatal("missing key must be refused")
	}
	// reconnect from a different OAuth client of the same principal
	other := human
	other.ClientID = "c2"
	if r := e.call("tasks/get", a.(string), other, nil); r["error"] != nil {
		t.Fatal("other client of the owner must read the task")
	}
}

func TestT7T8T9Ownership(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k1", "sleep")
	foreign := Principal{ID: "u2", Tenant: "other", ClientID: "c", Class: "human"}
	samet := Principal{ID: "u2", Tenant: "acme", ClientID: "c", Class: "human"}
	var bodies []string
	for _, m := range []string{"tasks/get", "tasks/update", "tasks/cancel"} {
		for _, p := range []Principal{foreign, samet} {
			bodies = append(bodies, fmt.Sprint(e.call(m, id, p, nil)))
		}
	}
	bodies = append(bodies, fmt.Sprint(e.call("tasks/get", "tk_unknown", human, nil)), fmt.Sprint(e.call("tasks/get", "%%bad", human, nil)))
	for _, b := range bodies {
		if b != bodies[0] || code(map[string]any{}) != 0 {
			t.Fatalf("non-identical refusal: %s vs %s", b, bodies[0])
		}
	}
	if code(e.call("tasks/get", id, foreign, nil)) != CodeInvalidParams {
		t.Fatal("want -32602")
	}
}

func TestT10Entropy(t *testing.T) {
	seen := map[string]bool{}
	for i := 0; i < 200000; i++ {
		id := RandomTaskID()
		if seen[id] {
			t.Fatal("collision")
		}
		seen[id] = true
		if len(id) != 3+26 {
			t.Fatalf("want 128 bits of entropy in the id, got %q", id)
		}
	}
}

func TestT11InputRoundTrip(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k1", "input")
	g := e.get(id)
	if g["status"] != "input_required" {
		t.Fatalf("%v", g)
	}
	ir := g["inputRequests"].(map[string]any)
	if len(ir) != 1 || ir[e.reqID(id)] == nil {
		t.Fatal("one elicitation keyed by request_id expected")
	}
	if g["pollIntervalMs"] != 30000 {
		t.Fatal("input_required polls at 30s")
	}
	e.answer(id, e.reqID(id), "ok", human)
	if g = e.get(id); g["status"] != "completed" || g["result"] == nil {
		t.Fatalf("%v", g)
	}
}

func TestT12BusinessErrorAndFailure(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k1", "input")
	e.answer(id, e.reqID(id), "fail", human)
	g := e.get(id)
	if g["status"] != "completed" || g["result"].(map[string]any)["isError"] != true {
		t.Fatalf("%v", g)
	}
	id2 := e.newTask("k2", "sleep")
	e.tw.Force("we_2", "Failed")
	if g = e.get(id2); g["status"] != "failed" || g["error"] == nil {
		t.Fatalf("%v", g)
	}
}

func TestT13UnreadableIs32603(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k1", "sleep")
	e.tw.FailReads = true
	if code(e.call("tasks/get", id, human, nil)) != CodeInternal {
		t.Fatal("temporal read failure must be -32603")
	}
	e.tw.FailReads, e.st.FailReads = false, true
	if code(e.call("tasks/get", id, human, nil)) != CodeInternal {
		t.Fatal("store read failure must be -32603")
	}
}

func TestT14LegacySurfaceAbsent(t *testing.T) {
	e := newEnv(t, "A")
	for _, m := range []string{"tasks/list", "tasks/result"} {
		if code(e.ad.Handle(Request{Method: m, Principal: human})) != CodeMethodNotFound {
			t.Fatal(m)
		}
	}
}

func TestN1N2ApprovalStaysWorking(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k1", "approval")
	g := e.get(id)
	if g["status"] != "working" || g["inputRequests"] != nil || g["statusMessage"] != "waiting for owner approval" {
		t.Fatalf("%v", g)
	}
	owner := human
	owner.ID = "owner-1"
	// task owner has no decide rights: no link. A decider reading would get one.
	e.st.Tasks[id].Owner = "owner-1"
	g = e.call("tasks/get", id, owner, nil)["result"].(map[string]any)
	if msg := g["statusMessage"].(string); len(msg) <= len("waiting for owner approval") {
		t.Fatalf("authorized decider should get the decide link: %q", msg)
	}
}

func TestN3N4HumanOnlyAndWrongRespondent(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k1", "human_input")
	if g := e.get(id); g["status"] != "working" || g["inputRequests"] != nil {
		t.Fatalf("%v", g)
	}
	id2 := e.newTask("k2", "input")
	bob := human
	bob.ActorRef = "bob"
	g := e.call("tasks/get", id2, bob, nil)["result"].(map[string]any)
	if g["status"] != "working" || g["inputRequests"] != nil {
		t.Fatalf("%v", g)
	}
}

func TestN5N6NeverReachesApproval(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k1", "approval")
	e.answer(id, "aar_1", "yes", human)
	e.answer(id, "anything", "yes", human)
	if len(e.tw.Signals) != 0 { // spy: no approve, no human_input_response
		t.Fatalf("unexpected signals %v", e.tw.Signals)
	}
	if e.tw.Grants["aar_1"].State != "pending" || e.tw.WFs["we_1"].Idx != 0 {
		t.Fatal("grant or workflow changed")
	}
	// replay of an answered key and a stale key
	id2 := e.newTask("k2", "input,approval")
	key := e.reqID(id2)
	e.answer(id2, key, "ok", human)
	n := len(e.tw.Signals)
	e.answer(id2, key, "ok", human)
	if len(e.tw.Signals) != n {
		t.Fatal("replayed key must not be forwarded")
	}
}

func TestN7Freshness(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k1", "input")
	key := e.reqID(id)
	e.tw.WFs["we_1"].Req.Round = 9
	e.answer(id, key, "ok", human)
	e.tw.WFs["we_1"].Req.Round = 1
	e.now = e.now.Add(100 * time.Hour)
	e.answer(id, key, "ok", human)
	if len(e.tw.Signals) != 0 || e.tw.WFs["we_1"].Idx != 0 {
		t.Fatal("expired or exhausted must not resume")
	}
	last := e.ad.Audit
	if last[len(last)-1] != "update entry ignored reason=expired" || last[len(last)-2] != "update entry ignored reason=rounds_exhausted" {
		t.Fatalf("audit: %v", last)
	}
	// hash changed between read and signal: the workflow re-check rejects it
	e2 := newEnv(t, "A")
	id = e2.newTask("k1", "input")
	e2.tw.BeforeSig = func(w *Workflow) { w.Req.Hash = "sha256:changed" }
	e2.answer(id, e2.reqID(id), "ok", human)
	if w := e2.tw.WFs["we_1"]; w.Idx != 0 || w.Rejections[0] != "hash_mismatch" {
		t.Fatalf("%v", w.Rejections)
	}
}

func TestN8N9Validation(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k1", "input")
	key := e.reqID(id)
	e.answer(id, key, string(make([]rune, 65)), human)
	if e.tw.WFs["we_1"].Idx != 0 {
		t.Fatal("over-long value must be refused")
	}
	for _, class := range []string{"service", "agent", "relayed"} {
		p := human
		p.Class = class
		if code(e.call("tasks/update", id, p, nil)) != CodeForbidden || code(e.call("tasks/cancel", id, p, nil)) != CodeForbidden {
			t.Fatalf("%s credential must be refused", class)
		}
	}
	// same_client policy refuses another client; default accepts and records it
	r := e.ad.Handle(Request{Method: "tools/call", Principal: human, Params: map[string]any{"_meta": meta(true),
		"arguments": map[string]any{"idempotency_key": "k2", "scenario": "input", "client_policy": "same_client"}}})
	id2 := r["result"].(map[string]any)["taskId"].(string)
	Dispatch(e.st, e.tw)
	other := human
	other.ClientID = "c2"
	e.answer(id2, e.reqID(id2), "ok", other)
	if e.tw.WFs["we_2"].Idx != 0 {
		t.Fatal("same_client must refuse another client")
	}
	e.answer(id, key, "ok", other)
	if e.tw.WFs["we_1"].Idx != 1 {
		t.Fatal("any_client_of_owner accepts it")
	}
}

func TestN10GrantBinding(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k1", "approval")
	if e.tw.DecideGrant("owner-1", "aar_1", "sha256:other", "sha256:ctx") != "mismatch" {
		t.Fatal("mismatch expected")
	}
	e.tw.Tick()
	if e.get(id)["status"] != "working" {
		t.Fatal("mismatched grant must not resume")
	}
	if e.tw.DecideGrant("u1", "aar_1", "sha256:art", "sha256:ctx") != "forbidden" {
		t.Fatal("non-decider must be refused")
	}
	e.tw.DecideGrant("owner-1", "aar_1", "sha256:art", "sha256:ctx")
	e.tw.Tick()
	e.tw.Tick()
	if g := e.get(id); g["status"] != "completed" || e.tw.Grants["aar_1"].State != "consumed" {
		t.Fatalf("%v", g)
	}
}

func TestCancel(t *testing.T) {
	// T15 while waiting for input: cancelled only after Canceled.
	e := newEnv(t, "A")
	id := e.newTask("k1", "input")
	ack := e.call("tasks/cancel", id, human, nil)["result"].(map[string]any)
	if ack["resultType"] != "complete" || e.get(id)["status"] != "cancelled" {
		t.Fatal("cancel at a gate")
	}
	// T16 activity: working with message, then cancelled.
	id2 := e.newTask("k2", "sleep")
	e.call("tasks/cancel", id2, human, nil)
	if g := e.get(id2); g["status"] != "working" || g["statusMessage"] != "cancellation requested" {
		t.Fatalf("%v", g)
	}
	e.tw.Tick()
	if e.get(id2)["status"] != "cancelled" {
		t.Fatal("cancelled after the boundary")
	}
	// T17 race: completion first wins; cancel after terminal is a no-op ack.
	id3 := e.newTask("k3", "sleep")
	e.tw.Tick()
	e.call("tasks/cancel", id3, human, nil)
	e.tw.Tick()
	if e.get(id3)["status"] != "completed" {
		t.Fatal("first terminal state wins")
	}
	// T18 before we_ exists.
	r := e.create(human, "k4", "sleep", true)["result"].(map[string]any)["taskId"].(string)
	e.call("tasks/cancel", r, human, nil)
	if g := e.get(r); g["status"] != "cancelled" {
		t.Fatalf("%v", g)
	}
	Dispatch(e.st, e.tw)
	if len(e.tw.WFs) != 3 {
		t.Fatal("a cancelled xr_ must never start a workflow")
	}
}

func TestT18RejectedOtherReasonFails(t *testing.T) {
	e := newEnv(t, "A")
	id := e.create(human, "k", "sleep", true)["result"].(map[string]any)["taskId"].(string)
	e.st.RejectPending(e.st.Tasks[id].XRID, "not_runnable")
	g := e.get(id)
	if g["status"] != "failed" || g["error"].(map[string]any)["data"].(map[string]any)["reason"] != "execution_request_rejected" {
		t.Fatalf("%v", g)
	}
}

func TestT19Revocation(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k1", "sleep")
	e.ad.Revoked["u1"] = true
	for _, m := range []string{"tasks/get", "tasks/update", "tasks/cancel"} {
		if code(e.call(m, id, human, nil)) != CodeInvalidParams {
			t.Fatal(m)
		}
	}
}

func TestTTL(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k1", "approval")
	// T21: 8-day approval wait; ttlMs null, never -32602 while running.
	for d := 0; d < 8; d++ {
		e.now = e.now.Add(24 * time.Hour)
		e.st.GC()
		if g := e.get(id); g["ttlMs"] != nil || g["status"] != "working" {
			t.Fatalf("day %d: %v", d, g)
		}
	}
	e.tw.DecideGrant("owner-1", "aar_1", "sha256:art", "sha256:ctx")
	e.tw.Tick()
	// T22: terminal ttlMs = (observed - created) + 24h.
	g := e.get(id)
	want := (8*24*time.Hour + TerminalRetention).Milliseconds()
	if g["status"] != "completed" || g["ttlMs"] != want {
		t.Fatalf("%v want ttl %d", g, want)
	}
	e.now = e.now.Add(TerminalRetention)
	if code(e.call("tasks/get", id, human, nil)) != CodeInvalidParams {
		t.Fatal("expired task must be -32602")
	}
	e.st.GC()
	// T23: the retained scope still binds the original expired id, but
	// tools/call must return -32602, not a fabricated fresh working handle.
	r := e.create(human, "k1", "approval", true)
	if code(r) != CodeInvalidParams || len(e.st.XRs) != 1 ||
		e.st.Scopes["acme|u1|probe|k1"].TaskID != id {
		t.Fatalf("expired retry must refuse without duplicate xr_: %v", r)
	}
	e.now = e.now.Add(XRReplayWindow)
	e.st.GC()
	if len(e.st.Scopes) != 0 {
		t.Fatal("scope row must be dropped after the window")
	}
	// T24: orphan-closed execution reads failed.
	id2 := e.newTask("k2", "sleep")
	e.tw.Force("we_2", "OrphanClosed")
	g = e.get(id2)
	if g["status"] != "failed" || g["error"].(map[string]any)["data"].(map[string]any)["reason"] != "execution_orphaned" {
		t.Fatalf("%v", g)
	}
}

func TestS9McpName(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k1", "sleep")
	other := e.newTask("k2", "sleep")
	for _, n := range []string{"", "TK_" + id[3:], id + " ", " " + id, other} {
		got := e.ad.Handle(Request{Method: "tasks/get", MCPName: n, Principal: human,
			Params: map[string]any{"taskId": id, "_meta": meta(true)}})
		if code(got) != CodeInvalidParams {
			t.Fatalf("Mcp-Name %q accepted", n)
		}
	}
}

func TestS10PartialUpdate(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k1", "input")
	r := e.call("tasks/update", id, human, map[string]any{"inputResponses": map[string]any{
		e.reqID(id): map[string]any{"action": "accept", "content": map[string]any{"value": "ok"}},
		"bogus":     map[string]any{"action": "accept", "content": map[string]any{"value": "x"}}}})
	if r["result"].(map[string]any)["resultType"] != "complete" || e.get(id)["status"] != "completed" {
		t.Fatalf("%v", r)
	}
}

func TestCheckInputRequestsRefusals(t *testing.T) {
	good := map[string]any{"hir-1": map[string]any{"method": "elicitation/create", "params": map[string]any{"mode": "form"}}}
	if CheckInputRequests(good) != nil {
		t.Fatal("good refused")
	}
	for name, m := range map[string]map[string]any{
		"empty":    {},
		"sampling": {"hir-1": map[string]any{"method": "sampling/createMessage"}},
		"approval": {"aar_1": good["hir-1"]},
		"url":      {"hir-1": map[string]any{"method": "elicitation/create", "params": map[string]any{"mode": "url"}}},
	} {
		if CheckInputRequests(m) == nil {
			t.Fatalf("%s accepted", name)
		}
	}
}

// Durable create (P1-TS-02). [MODEL] only: not a Postgres atomicity proof.
func TestD1D4CrashPoints(t *testing.T) {
	for _, tc := range []struct {
		design string
		steps  int
	}{{"A", 8}, {"B", 5}} {
		for k := 1; k <= tc.steps; k++ {
			e := newEnv(t, tc.design)
			e.st.CrashAt = k
			in := TaskIn{Tenant: "acme", Owner: "u1", Tool: "probe", Key: "k", ArgsDigest: "d", Scenario: "sleep"}
			_, _, err := e.st.CreateTask(in)
			if err == nil {
				t.Fatalf("%s step %d: crash not injected", tc.design, k)
			}
			if tc.design == "A" && k < 8 && (len(e.st.XRs) != 0 || len(e.st.Tasks) != 0 || len(e.st.Scopes) != 0) {
				t.Fatalf("A step %d left state behind", k)
			}
			e.st.CrashAt = 0
			id, _, err := e.st.CreateTask(in)
			if err != nil || len(e.st.XRs) != 1 || e.st.Tasks[id] == nil {
				t.Fatalf("%s step %d: retry err=%v xrs=%d", tc.design, k, err, len(e.st.XRs))
			}
			id2, _, _ := e.st.CreateTask(in)
			if id2 != id {
				t.Fatalf("%s step %d: retry returned another id", tc.design, k)
			}
		}
	}
}

func TestD4SweepBindsStale(t *testing.T) {
	e := newEnv(t, "B")
	e.st.CrashAt = 3 // after wi_, before xr_
	in := TaskIn{Tenant: "acme", Owner: "u1", Tool: "probe", Key: "k", ArgsDigest: "d"}
	e.st.CreateTask(in)
	e.st.CrashAt = 0
	if e.st.Sweep(time.Hour) != 0 {
		t.Fatal("fresh reserving rows are left alone")
	}
	e.now = e.now.Add(2 * time.Hour)
	if e.st.Sweep(time.Hour) != 1 || len(e.st.Tasks) != 1 || len(e.st.XRs) != 1 {
		t.Fatal("sweep must bind the stale row with exactly one xr_")
	}
}

func TestD2ConcurrentRace(t *testing.T) {
	for _, design := range []string{"A", "B"} {
		e := newEnv(t, design)
		var wg sync.WaitGroup
		ids := make([]string, 100)
		for i := range ids {
			wg.Add(1)
			go func(i int) {
				defer wg.Done()
				ids[i], _, _ = e.st.CreateTask(TaskIn{Tenant: "acme", Owner: "u1", Tool: "probe", Key: "k", ArgsDigest: "d"})
			}(i)
		}
		wg.Wait()
		for _, id := range ids {
			if id != ids[0] {
				t.Fatalf("%s: callers disagree", design)
			}
		}
		if len(e.st.XRs) != 1 {
			t.Fatalf("%s: %d xr_", design, len(e.st.XRs))
		}
	}
}

func TestD3ClaimScanNeverSeesUnboundXR(t *testing.T) {
	e := newEnv(t, "A")
	stop := make(chan struct{})
	bad := make(chan []string, 1)
	var scan sync.WaitGroup
	scan.Add(1)
	go func() {
		defer scan.Done()
		for {
			select {
			case <-stop:
				return
			default:
				if o := e.st.OrphanPending(); len(o) > 0 {
					select {
					case bad <- o:
					default:
					}
					return
				}
			}
		}
	}()
	var wg sync.WaitGroup
	for i := 0; i < 200; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			e.st.CreateTask(TaskIn{Tenant: "acme", Owner: "u1", Tool: "probe", Key: fmt.Sprint(i), ArgsDigest: "d"})
		}(i)
	}
	wg.Wait()
	close(stop)
	scan.Wait()
	select {
	case o := <-bad:
		t.Fatalf("scan saw unbound xr_: %v", o)
	default:
	}
	// Contrast: design B has a window (reserving row, xr_ exists, unbound).
	b := newEnv(t, "B")
	b.st.CrashAt = 4
	b.st.CreateTask(TaskIn{Tenant: "acme", Owner: "u1", Tool: "probe", Key: "k", ArgsDigest: "d"})
	if len(b.st.OrphanPending()) != 1 {
		t.Fatal("design B window not modelled")
	}
}

// TestFixtures regenerates (UPDATE_FIXTURES=1) or verifies the golden
// responses the Python checker validates against the schema (S8).
func TestFixtures(t *testing.T) {
	e := newEnv(t, "A")
	n := 0
	e.st.NewTaskID = func() string { n++; return fmt.Sprintf("tk_fixture%02d", n) }
	out := map[string]any{}
	grab := func(name string, r map[string]any) { out[name] = r }

	grab("create", e.create(human, "k1", "sleep", true))
	Dispatch(e.st, e.tw)
	grab("get_working", e.call("tasks/get", "tk_fixture01", human, nil))
	e.tw.Tick()
	grab("get_completed", e.call("tasks/get", "tk_fixture01", human, nil))

	e.create(human, "k2", "input", true)
	Dispatch(e.st, e.tw)
	grab("get_input_required", e.call("tasks/get", "tk_fixture02", human, nil))
	grab("update_ack", e.call("tasks/update", "tk_fixture02", human, map[string]any{"inputResponses": map[string]any{}}))
	grab("cancel_ack", e.call("tasks/cancel", "tk_fixture02", human, nil))
	grab("get_cancelled", e.call("tasks/get", "tk_fixture02", human, nil))

	e.create(human, "k3", "approval", true)
	Dispatch(e.st, e.tw)
	grab("get_working_approval", e.call("tasks/get", "tk_fixture03", human, nil))
	e.tw.Force("we_3", "Failed")
	grab("get_failed", e.call("tasks/get", "tk_fixture03", human, nil))

	grab("error_32602", e.call("tasks/get", "tk_nope", human, nil))
	e.tw.FailReads = true
	grab("error_32603", e.call("tasks/get", "tk_fixture01", human, nil))
	e.tw.FailReads = false
	grab("error_32021", e.ad.Handle(Request{Method: "tasks/get", MCPName: "x", Principal: human, Params: map[string]any{"taskId": "x"}}))

	dir := filepath.Join("..", "fixtures", "responses")
	for name, v := range out {
		b, _ := json.MarshalIndent(v, "", "  ")
		b = append(b, '\n')
		path := filepath.Join(dir, name+".json")
		if os.Getenv("UPDATE_FIXTURES") == "1" {
			if err := os.WriteFile(path, b, 0o644); err != nil {
				t.Fatal(err)
			}
			continue
		}
		have, err := os.ReadFile(path)
		if err != nil || string(have) != string(b) {
			t.Fatalf("fixture %s is stale; rerun with UPDATE_FIXTURES=1", name)
		}
	}
}

// Review fix: the dispatcher claims the xr_ between cancel's read and its
// RejectPending. The cancel must still reach the workflow it started.
func TestCancelRaceWithDispatcherClaim(t *testing.T) {
	e := newEnv(t, "A")
	id := e.create(human, "k1", "input", true)["result"].(map[string]any)["taskId"].(string)
	// A dispatcher that claims and starts but knows nothing of recorded cancels:
	// only the adapter's re-read can save the cancel here.
	e.ad.AfterXRRead = func() {
		x := e.st.PendingXRs()[0]
		we := "we_" + x.ID[3:]
		if !e.st.Claim(x.ID, we) {
			t.Fatal("claim must win in this interleaving")
		}
		e.tw.Start(we, []string{"input"}, []string{"alice"})
	}
	e.call("tasks/cancel", id, human, nil)
	if g := e.get(id); g["status"] != "cancelled" {
		t.Fatalf("cancel lost in the claim race: %v", g)
	}
}

// Claim happened but Start has not: Dispatch honors the recorded cancel.
func TestCancelRecordedBeforeStartIsHonored(t *testing.T) {
	e := newEnv(t, "A")
	id := e.create(human, "k1", "input", true)["result"].(map[string]any)["taskId"].(string)
	now := e.now
	e.st.Update(id, func(r *TaskRow) { r.CancelRequestedAt = &now })
	Dispatch(e.st, e.tw)
	if g := e.get(id); g["status"] != "cancelled" {
		t.Fatalf("recorded cancel ignored at start: %v", g)
	}
}

// Review fix: a form-mode decline is a valid response and is forwarded;
// the workflow ends as a business error, never as a silent no-op.
func TestDeclineIsForwarded(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k1", "input")
	e.call("tasks/update", id, human, map[string]any{"inputResponses": map[string]any{
		e.reqID(id): map[string]any{"action": "decline"}}})
	g := e.get(id)
	if g["status"] != "completed" {
		t.Fatalf("decline not forwarded: %v", g)
	}
	if r, _ := g["result"].(map[string]any); r == nil || r["isError"] != true {
		t.Fatalf("decline must end as a business error: %v", g)
	}
}

// Decline still passes checks 2-7: wrong respondent is ignored.
func TestDeclineStillChecked(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k1", "input")
	other := human
	other.ActorRef = "mallory"
	e.call("tasks/update", id, other, map[string]any{"inputResponses": map[string]any{
		e.reqID(id): map[string]any{"action": "decline"}}})
	if g := e.get(id); g["status"] != "input_required" {
		t.Fatalf("decline from a non-respondent must be ignored: %v", g)
	}
}


// A policy mutation between adapter validation and Temporal signalling must
// still be denied by the sealed workflow-side respondent policy.
func TestSealedRespondentPolicyRevalidatedAtSignal(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k-policy", "input")
	e.tw.BeforeSig = func(w *Workflow) { w.Req.AllowMCP = false }
	e.answer(id, e.reqID(id), "ok", human)
	w := e.tw.WFs["we_1"]
	if w.Idx != 0 || len(w.Rejections) != 1 || w.Rejections[0] != "policy_denied" {
		t.Fatalf("stale client-relay policy passed workflow recheck: %+v", w)
	}
}

func TestSealedClientAndAssuranceRevalidatedAtSignal(t *testing.T) {
	e := newEnv(t, "A")
	result := e.ad.Handle(Request{Method: "tools/call", Principal: human, Params: map[string]any{
		"_meta": meta(true),
		"arguments": map[string]any{"idempotency_key": "k-client-recheck", "scenario": "input", "client_policy": "same_client"},
	}})
	id := result["result"].(map[string]any)["taskId"].(string)
	Dispatch(e.st, e.tw)
	e.tw.BeforeSig = func(w *Workflow) { w.Req.ClientID = "different-client" }
	e.answer(id, e.reqID(id), "ok", human)
	w := e.tw.WFs["we_1"]
	if w.Idx != 0 || len(w.Rejections) != 1 || w.Rejections[0] != "client_mismatch" {
		t.Fatalf("stale client policy passed workflow recheck: %+v", w)
	}
	e2 := newEnv(t, "A")
	e2.newTask("k-assurance", "input")
	req := e2.tw.WFs["we_1"].Req
	e2.tw.SignalHumanInput("we_1", HIResponse{RequestID: req.RequestID, RequestHash: req.Hash,
		Value: "ok", Respondent: human.ActorRef, Client: human.ClientID,
		Surface: "mcp", Assurance: "service_token"})
	w = e2.tw.WFs["we_1"]
	if w.Idx != 0 || len(w.Rejections) != 1 || w.Rejections[0] != "assurance_mismatch" {
		t.Fatalf("spoofed assurance accepted: %+v", w)
	}
}

func TestRevokedCreateAndSideEffectCheckpoint(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k-revoke", "sleep")
	e.ad.Revoked[human.ID] = true
	if code(e.create(human, "k-after-revoke", "sleep", true)) != CodeForbidden {
		t.Fatal("revoked principal created a new task")
	}
	if len(e.st.XRs) != 1 {
		t.Fatal("revocation did not fence creation")
	}
	e.tw.Tick()
	w := e.tw.WFs["we_1"]
	if w.State != "Failed" || w.FailedReason != "authorization_revoked" || w.SideEffect {
		t.Fatalf("revoked task reached side-effect checkpoint: %+v", w)
	}
	delete(e.ad.Revoked, human.ID)
	if got := e.get(id); got["status"] != "failed" ||
		got["error"].(map[string]any)["data"].(map[string]any)["reason"] != "authorization_revoked" {
		t.Fatalf("revocation failure not classified: %v", got)
	}
}

func TestExpiredReplayRefusesFabricatedHandle(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k-expire-retry", "sleep")
	e.tw.Tick()
	if got := e.get(id); got["status"] != "completed" {
		t.Fatalf("not terminal: %v", got)
	}
	e.now = e.now.Add(TerminalRetention)
	xrCount := len(e.st.XRs)
	if got := e.create(human, "k-expire-retry", "sleep", true); code(got) != CodeInvalidParams {
		t.Fatalf("pre-GC expired retry returned a fabricated working handle: %v", got)
	}
	e.st.GC()
	if got := e.create(human, "k-expire-retry", "sleep", true); code(got) != CodeInvalidParams {
		t.Fatalf("post-GC expired retry returned a fabricated working handle: %v", got)
	}
	if len(e.st.XRs) != xrCount || e.st.Scopes["acme|u1|probe|k-expire-retry"].TaskID != id {
		t.Fatal("replay created a second execution or lost the retained id")
	}
	if code(e.call("tasks/get", id, human, nil)) != CodeInvalidParams {
		t.Fatal("expired task must remain inaccessible")
	}
	e.st.FailReads = true
	if got := e.create(human, "k-expire-retry", "sleep", true); code(got) != CodeInternal {
		t.Fatalf("store read error hidden on create replay: %v", got)
	}
}

func TestDeclineIsNotAnAcceptedEmptyValue(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k-decline-distinct", "input")
	req := e.reqID(id)
	e.call("tasks/update", id, human, map[string]any{"inputResponses": map[string]any{
		req: map[string]any{"action": "decline"},
	}})
	w := e.tw.WFs["we_1"]
	if len(w.Declines) != 1 || w.Declines[0] != req || len(w.Answers) != 0 {
		t.Fatalf("decline became an accepted value: %+v", w)
	}
}

func TestDeclineWithUnexpectedContentRejected(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k-invalid-decline", "input")
	e.call("tasks/update", id, human, map[string]any{"inputResponses": map[string]any{
		e.reqID(id): map[string]any{"action": "decline", "content": map[string]any{"value": "unexpected"}},
	}})
	if w := e.tw.WFs["we_1"]; w.Idx != 0 || len(w.Answers) != 0 || len(w.Declines) != 0 {
		t.Fatalf("malformed decline resumed execution: %+v", w)
	}
}

func TestDeclineStopsBeforeSubsequentSideEffect(t *testing.T) {
	e := newEnv(t, "A")
	id := e.newTask("k-decline-stop", "input,sleep")
	e.call("tasks/update", id, human, map[string]any{"inputResponses": map[string]any{
		e.reqID(id): map[string]any{"action": "decline"},
	}})
	e.tw.Tick()
	w := e.tw.WFs["we_1"]
	if w.State != "Completed" || !w.Business || w.SideEffect || len(w.Declines) != 1 {
		t.Fatalf("decline continued into a later effect: %+v", w)
	}
	if g := e.get(id); g["status"] != "completed" || g["result"].(map[string]any)["isError"] != true {
		t.Fatalf("decline outcome not reported as business refusal: %v", g)
	}
}
