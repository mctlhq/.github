package model

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"strings"
	"time"
	"unicode/utf8"
)

// Extension is the negotiated MCP Tasks extension id.
const Extension = "io.modelcontextprotocol/tasks"

// JSON-RPC error codes used by the adapter.
const (
	CodeMethodNotFound = -32601
	CodeInvalidParams  = -32602
	CodeInternal       = -32603
	CodeForbidden      = -32001
	CodeMissingCap     = -32021
)

// Principal is the authenticated caller.
type Principal struct {
	ID, Tenant, ClientID, ActorRef string
	Class                          string // human | service | agent | relayed
}

// Request is one MCP request, already authenticated.
type Request struct {
	Method    string
	Params    map[string]any
	MCPName   string // Mcp-Name header; "" means missing
	Principal Principal
}

// Adapter is the protocol adapter. It never starts Temporal (the dispatcher
// does) and has no status column: status is derived on every read.
type Adapter struct {
	Store    *Store
	Temporal *Temporal
	Now      func() time.Time
	Revoked  map[string]bool
	Audit    []string // reason codes only, never content

	// AfterXRRead is a test hook between the xr_ read and RejectPending in cancel.
	AfterXRRead func()
}

// declared reports whether the request itself declares the extension.
func declared(p map[string]any) bool {
	m, _ := p["_meta"].(map[string]any)
	c, _ := m["io.modelcontextprotocol/clientCapabilities"].(map[string]any)
	e, _ := c["extensions"].(map[string]any)
	_, ok := e[Extension]
	return ok
}

func rpcErr(code int, msg string) map[string]any {
	return map[string]any{"error": map[string]any{"code": code, "message": msg}}
}

func (a *Adapter) audit(format string, args ...any) {
	a.Audit = append(a.Audit, fmt.Sprintf(format, args...))
}

// Handle dispatches one request and returns a JSON-RPC-shaped map.
func (a *Adapter) Handle(r Request) map[string]any {
	switch r.Method {
	case "tools/call":
		return a.call(r)
	case "tasks/get", "tasks/update", "tasks/cancel":
		return a.taskCall(r)
	}
	// tasks/list, tasks/result and the legacy SEP-1686 set are not served.
	return rpcErr(CodeMethodNotFound, "method not found")
}

// ArgsDigest hashes canonical arguments without _meta.
func ArgsDigest(args map[string]any) string {
	c := map[string]any{}
	for k, v := range args {
		if k != "_meta" {
			c[k] = v
		}
	}
	b, _ := json.Marshal(c) // encoding/json sorts map keys
	return fmt.Sprintf("sha256:%x", sha256.Sum256(b))
}

func (a *Adapter) call(r Request) map[string]any {
	if a.Revoked[r.Principal.ID] {
		a.audit("create refused reason=revoked")
		return rpcErr(CodeForbidden, "principal revoked")
	}
	args, _ := r.Params["arguments"].(map[string]any)
	key, _ := args["idempotency_key"].(string)
	if key == "" {
		a.audit("create refused reason=missing_idempotency_key")
		return rpcErr(CodeInvalidParams, "idempotency_key is required")
	}
	scenario, _ := args["scenario"].(string)
	policy, _ := args["client_policy"].(string)
	if policy == "" {
		policy = "any_client_of_owner"
	}
	id, _, err := a.Store.CreateTask(TaskIn{Tenant: r.Principal.Tenant, Owner: r.Principal.ID,
		ClientID: r.Principal.ClientID, Tool: "probe", Key: key, ArgsDigest: ArgsDigest(args),
		ClientPolicy: policy, Scenario: scenario})
	if err != nil {
		if err == ErrKeyReused {
			a.audit("create refused reason=idempotency_key_reused")
			return rpcErr(CodeInvalidParams, "idempotency_key_reused")
		}
		return rpcErr(CodeInternal, "internal error")
	}
	t, ok, err := a.Store.Task(id)
	if err != nil {
		return rpcErr(CodeInternal, "internal error")
	}
	if !ok || (t.TerminalObservedAt != nil &&
		!a.Now().Before(t.TerminalObservedAt.Add(TerminalRetention))) {
		// A retained idempotency scope prevents duplicate dispatch while a
		// terminal task is expired, whether or not GC has deleted its row.
		// Never fabricate a fresh working handle for an expired task.
		a.audit("create refused reason=expired_task_replay")
		return rpcErr(CodeInvalidParams, "unknown task")
	}
	a.audit("create task=%s", id)
	if !declared(r.Params) {
		return map[string]any{"result": map[string]any{
			"content":           []any{map[string]any{"type": "text", "text": "started"}},
			"structuredContent": map[string]any{"work_item_id": t.WIID, "execution_request_id": t.XRID},
		}}
	}
	return map[string]any{"result": map[string]any{
		"resultType": "task", "taskId": id, "status": "working",
		"createdAt": ts(t.CreatedAt), "lastUpdatedAt": ts(t.CreatedAt),
		"ttlMs": nil, "pollIntervalMs": 5000,
	}}
}

func ts(t time.Time) string { return t.UTC().Format(time.RFC3339) }

// lookup re-authorizes and resolves a task. Every failure is the same -32602.
func (a *Adapter) lookup(r Request) (*TaskRow, map[string]any) {
	id, _ := r.Params["taskId"].(string)
	notFound := rpcErr(CodeInvalidParams, "unknown task")
	if a.Revoked[r.Principal.ID] {
		a.audit("refused reason=revoked")
		return nil, notFound
	}
	t, ok, err := a.Store.Task(id)
	if err != nil {
		return nil, rpcErr(CodeInternal, "internal error")
	}
	if !ok || t.Tenant != r.Principal.Tenant || t.Owner != r.Principal.ID {
		a.audit("refused reason=not_found")
		return nil, notFound
	}
	if t.TerminalObservedAt != nil && !a.Now().Before(t.TerminalObservedAt.Add(TerminalRetention)) {
		a.audit("refused reason=expired")
		return nil, notFound
	}
	return t, nil
}

func (a *Adapter) taskCall(r Request) map[string]any {
	if !declared(r.Params) {
		return rpcErr(CodeMissingCap, "tasks extension not declared on this request")
	}
	// Mcp-Name must equal params.taskId byte for byte, before any lookup.
	if id, _ := r.Params["taskId"].(string); r.MCPName == "" || r.MCPName != id {
		return rpcErr(CodeInvalidParams, "unknown task")
	}
	t, e := a.lookup(r)
	if e != nil {
		return e
	}
	switch r.Method {
	case "tasks/get":
		return a.get(t, r.Principal)
	case "tasks/cancel":
		return a.cancel(t, r.Principal)
	}
	return a.update(t, r)
}

func (a *Adapter) get(t *TaskRow, p Principal) map[string]any {
	v, e := a.view(t, p)
	if e != nil {
		return e
	}
	return map[string]any{"result": v}
}

// view derives the task state. First matching row of design section 3 wins.
func (a *Adapter) view(t *TaskRow, p Principal) (map[string]any, map[string]any) {
	xr, err := a.Store.XR(t.XRID)
	if err != nil || xr == nil {
		return nil, rpcErr(CodeInternal, "internal error")
	}
	out := map[string]any{"resultType": "complete", "taskId": t.TaskID, "createdAt": ts(t.CreatedAt),
		"pollIntervalMs": 5000, "ttlMs": nil}
	status, msg := "working", ""
	terminal := false
	switch {
	case xr.State == "rejected" && xr.Reason == "cancelled_by_task":
		status, terminal = "cancelled", true
	case xr.State == "rejected":
		status, terminal = "failed", true
		out["error"] = map[string]any{"code": CodeInternal, "message": "execution request rejected",
			"data": map[string]any{"reason": "execution_request_rejected"}}
	case xr.WEID == "":
		status = "working"
	default:
		w, err := a.Temporal.Describe(xr.WEID)
		if err != nil {
			return nil, rpcErr(CodeInternal, "internal error")
		}
		switch w.State {
		case "Completed":
			status, terminal = "completed", true
			res := map[string]any{"content": []any{map[string]any{"type": "text", "text": "done"}}}
			if w.Business {
				res["isError"] = true
			}
			out["result"] = res
		case "Canceled":
			status, terminal = "cancelled", true
		case "Failed", "TimedOut", "Terminated":
			status, terminal = "failed", true
			reason := "execution_" + strings.ToLower(w.State)
			if w.FailedReason != "" {
				reason = w.FailedReason
			}
			out["error"] = map[string]any{"code": CodeInternal, "message": "execution " + strings.ToLower(w.State),
				"data": map[string]any{"reason": reason}}
		case "OrphanClosed":
			status, terminal = "failed", true
			out["error"] = map[string]any{"code": CodeInternal, "message": "execution orphaned",
				"data": map[string]any{"reason": "execution_orphaned"}}
		default:
			status, msg = a.gate(&w, t, p, out)
		}
	}
	if status == "working" && t.CancelRequestedAt != nil && msg == "" {
		msg = "cancellation requested"
	}
	if terminal && t.TerminalObservedAt == nil {
		now := a.Now()
		a.Store.Update(t.TaskID, func(r *TaskRow) { r.TerminalObservedAt = &now })
		t.TerminalObservedAt = &now
	}
	if terminal {
		out["ttlMs"] = t.TerminalObservedAt.Sub(t.CreatedAt).Milliseconds() + TerminalRetention.Milliseconds()
	}
	out["status"] = status
	if status == "input_required" {
		out["pollIntervalMs"] = 30000
	}
	if msg != "" {
		out["statusMessage"] = msg
	}
	last := t.CreatedAt
	if t.TerminalObservedAt != nil {
		last = *t.TerminalObservedAt
	}
	out["lastUpdatedAt"] = ts(last)
	return out, nil
}

// gate maps a running workflow's gate. An approval never becomes
// input_required; human-only input stays working.
func (a *Adapter) gate(w *Workflow, t *TaskRow, p Principal, out map[string]any) (string, string) {
	switch w.Gate {
	case GateInput:
		if w.Req.AllowMCP && contains(w.Req.ActorRefs, p.ActorRef) {
			ir := map[string]any{w.Req.RequestID: map[string]any{
				"method": "elicitation/create",
				"params": map[string]any{"mode": "form", "message": w.Req.Message,
					"requestedSchema": map[string]any{"type": "object", "required": []any{"value"},
						"properties": map[string]any{"value": map[string]any{"type": "string", "maxLength": w.Req.MaxLen}}}}}}
			if err := CheckInputRequests(ir); err == nil {
				out["inputRequests"] = ir
				return "input_required", ""
			}
		}
		return "working", "waiting for human input on another surface"
	case GateApproval:
		msg := "waiting for owner approval"
		if w.Grant != nil && w.Grant.Deciders[p.ID] {
			msg += "; decide at https://decide.example.invalid/" + w.Grant.ID
		}
		return "working", msg
	case GateTimedOut:
		return "working", ""
	}
	return "working", ""
}

// CheckInputRequests refuses anything but a non-empty form-mode elicitation
// map keyed by a request id (never an aar_ id, never sampling).
func CheckInputRequests(m map[string]any) error {
	if len(m) == 0 {
		return fmt.Errorf("inputRequests must be non-empty")
	}
	for k, v := range m {
		if strings.HasPrefix(k, "aar_") {
			return fmt.Errorf("approval-shaped key %q", k)
		}
		e, _ := v.(map[string]any)
		if e["method"] != "elicitation/create" {
			return fmt.Errorf("only elicitation/create is allowed, got %v", e["method"])
		}
		if pr, _ := e["params"].(map[string]any); pr["mode"] != "form" {
			return fmt.Errorf("only form mode is allowed")
		}
	}
	return nil
}

func (a *Adapter) cancel(t *TaskRow, p Principal) map[string]any {
	if p.Class != "human" {
		a.audit("cancel refused reason=credential_class")
		return rpcErr(CodeForbidden, "credential class not allowed")
	}
	ack := map[string]any{"result": map[string]any{"resultType": "complete"}}
	xr, err := a.Store.XR(t.XRID)
	if err != nil || xr == nil {
		return rpcErr(CodeInternal, "internal error")
	}
	now := a.Now()
	a.Store.Update(t.TaskID, func(r *TaskRow) {
		if r.CancelRequestedAt == nil {
			r.CancelRequestedAt = &now
		}
	})
	a.audit("cancel task=%s", t.TaskID)
	if xr.WEID == "" {
		if a.AfterXRRead != nil {
			a.AfterXRRead()
		}
		if a.Store.RejectPending(xr.ID, "cancelled_by_task") {
			return ack
		}
		// The dispatcher claimed the xr_ between the read and the reject:
		// re-read and cancel the workflow it started. If Start has not run
		// yet, RequestCancel is a no-op and Dispatch honors the recorded
		// CancelRequestedAt right after Start.
		if xr, err = a.Store.XR(t.XRID); err != nil || xr == nil {
			return rpcErr(CodeInternal, "internal error")
		}
		if xr.WEID == "" {
			return ack // rejected by someone else: terminal already
		}
	}
	a.Temporal.RequestCancel(xr.WEID) // no-op on a terminal workflow
	return ack
}

func (a *Adapter) update(t *TaskRow, r Request) map[string]any {
	p := r.Principal
	ack := map[string]any{"result": map[string]any{"resultType": "complete"}}
	// 1. credential class: refuses the whole call.
	if p.Class != "human" {
		a.audit("update refused reason=credential_class")
		return rpcErr(CodeForbidden, "credential class not allowed")
	}
	resp, _ := r.Params["inputResponses"].(map[string]any)
	xr, err := a.Store.XR(t.XRID)
	if err != nil || xr == nil {
		return rpcErr(CodeInternal, "internal error")
	}
	var w Workflow
	if xr.WEID != "" {
		if w, err = a.Temporal.Describe(xr.WEID); err != nil {
			return rpcErr(CodeInternal, "internal error")
		}
	}
	for key, raw := range resp {
		reason := a.checkEntry(t, p, &w, key, raw)
		if reason != "" {
			a.audit("update entry ignored reason=%s", reason)
			continue
		}
		e := raw.(map[string]any)
		c, _ := e["content"].(map[string]any)
		val, _ := c["value"].(string)
		// 8. forward; the workflow re-validates everything.
		a.Temporal.SignalHumanInput(xr.WEID, HIResponse{RequestID: w.Req.RequestID, RequestHash: w.Req.Hash,
			Value: val, Declined: e["action"] == "decline", Respondent: p.ActorRef, Surface: "mcp", Assurance: "client_relayed", Client: p.ClientID})
		a.audit("update entry forwarded client=%s", p.ClientID)
	}
	return ack
}

// checkEntry runs checks 2-7 and returns a reason code, or "" if accepted.
func (a *Adapter) checkEntry(t *TaskRow, p Principal, w *Workflow, key string, raw any) string {
	if w == nil || w.State != "Running" || w.Req == nil || w.Gate != GateInput && w.Gate != GateTimedOut {
		return "not_open"
	}
	rq := w.Req
	if key != rq.RequestID { // covers aar_ ids, stale and arbitrary keys
		return "not_open"
	}
	if !rq.AllowMCP {
		return "policy_denied"
	}
	if t.ClientPolicy == "same_client" && p.ClientID != t.ClientID {
		return "client_mismatch"
	}
	if !contains(rq.ActorRefs, p.ActorRef) {
		return "respondent_mismatch"
	}
	if !a.Now().Before(rq.ExpiresAt) {
		return "expired"
	}
	if rq.Round > rq.MaxRound {
		return "rounds_exhausted"
	}
	e, ok := raw.(map[string]any)
	if !ok {
		return "invalid_value"
	}
	if e["action"] == "decline" {
		if _, hasContent := e["content"]; hasContent {
			return "invalid_value"
		}
		return "" // explicit decline, never an accepted empty value
	}
	c, _ := e["content"].(map[string]any)
	val, ok := c["value"].(string)
	b, _ := json.Marshal(c)
	if e["action"] != "accept" || !ok || utf8.RuneCountInString(val) > rq.MaxLen ||
		utf8.RuneCountInString(val) > 2000 || len(b) > 8000 {
		return "invalid_value"
	}
	return ""
}
