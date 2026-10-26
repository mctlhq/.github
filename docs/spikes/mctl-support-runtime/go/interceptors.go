package main

import (
	"context"
	"fmt"
	"log"
	"strings"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
)

// bearerAuth is the SDK's own auth hook: a CallInterceptor that runs before
// every RequestHandler method, reads the Authorization service param and
// attaches the principal to the CallContext. The executor later reads it from
// ExecutorContext.User (copied from the CallContext by agentexec.go).
type bearerAuth struct {
	a2asrv.PassthroughCallInterceptor
}

func (bearerAuth) Before(ctx context.Context, callCtx *a2asrv.CallContext, req *a2asrv.Request) (context.Context, any, error) {
	values, ok := callCtx.ServiceParams().Get("authorization")
	if !ok || len(values) == 0 || values[0] == "" {
		return ctx, nil, fmt.Errorf("bearer token required: %w", a2a.ErrUnauthenticated)
	}
	token := strings.TrimSpace(strings.TrimPrefix(values[0], "Bearer "))
	principal, ok := strings.CutPrefix(token, "spike-")
	if !ok || principal == "" {
		return ctx, nil, fmt.Errorf("foreign bearer token: %w", a2a.ErrUnauthorized)
	}
	callCtx.User = a2asrv.NewAuthenticatedUser(principal, nil)
	return ctx, nil, nil
}

// contextOwnerGuard closes two gaps the SDK leaves open on SendMessage:
//   - a client-supplied contextId is accepted as-is by agentexec.go
//     createNewExecutionContext, so another principal could append a task to
//     a foreign context; the guard refuses with the same 404 the task store
//     uses for foreign tasks (A2A spec 3.3.2 masking);
//   - a repeated messageId opens a second task (no dedup in the SDK); the
//     guard answers with the task that already holds that message.
type contextOwnerGuard struct {
	a2asrv.PassthroughCallInterceptor
	store *pgStore
}

func (g *contextOwnerGuard) Before(ctx context.Context, callCtx *a2asrv.CallContext, req *a2asrv.Request) (context.Context, any, error) {
	send, ok := req.Payload.(*a2a.SendMessageRequest)
	if !ok || send == nil || send.Message == nil {
		return ctx, nil, nil
	}
	msg := send.Message
	if msg.ID != "" {
		taskID, found, err := g.store.taskForMessage(ctx, msg.ID)
		if err != nil {
			return ctx, nil, fmt.Errorf("message index: %w", err)
		}
		if found {
			stored, err := g.store.Get(ctx, taskID)
			if err != nil {
				return ctx, nil, fmt.Errorf("duplicate message task: %w", err)
			}
			return ctx, a2a.SendMessageResult(stored.Task), nil
		}
	}
	if msg.ContextID != "" {
		owner, found, err := g.store.contextOwner(ctx, msg.ContextID)
		if err != nil {
			return ctx, nil, fmt.Errorf("context lookup: %w", err)
		}
		if found && owner != callCtx.User.Name {
			return ctx, nil, fmt.Errorf("context: %w", a2a.ErrTaskNotFound)
		}
	}
	return ctx, nil, nil
}

type traceKey struct{}

// traceInterceptor is the OTel-style hook: one line per request with the
// task id and the principal, emitted from the SDK's interceptor chain.
type traceInterceptor struct{ replica string }

func (traceInterceptor) Before(ctx context.Context, callCtx *a2asrv.CallContext, req *a2asrv.Request) (context.Context, any, error) {
	var id a2a.TaskID
	switch v := req.Payload.(type) {
	case *a2a.GetTaskRequest:
		id = v.ID
	case *a2a.CancelTaskRequest:
		id = v.ID
	case *a2a.SubscribeToTaskRequest:
		id = v.ID
	case *a2a.SendMessageRequest:
		if v.Message != nil {
			id = v.Message.TaskID
		}
	}
	return context.WithValue(ctx, traceKey{}, id), nil, nil
}

func (t traceInterceptor) After(ctx context.Context, callCtx *a2asrv.CallContext, resp *a2asrv.Response) error {
	id, _ := ctx.Value(traceKey{}).(a2a.TaskID)
	switch v := resp.Payload.(type) {
	case *a2a.Task:
		id = v.ID
	case *a2a.Message:
		id = v.TaskID
	}
	principal := "-"
	if callCtx.User != nil && callCtx.User.Authenticated {
		principal = callCtx.User.Name
	}
	status := "ok"
	if resp.Err != nil {
		status = "err=" + resp.Err.Error()
	}
	log.Printf("trace task=%s principal=%s method=%s replica=%s %s", id, principal, callCtx.Method(), t.replica, status)
	return nil
}
