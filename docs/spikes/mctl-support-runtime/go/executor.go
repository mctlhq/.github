package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"iter"
	"log"
	"strings"
	"time"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
)

// --- model and tool abstractions (what a real Anthropic-backed loop would plug into) ---

type toolCall struct {
	Name string
	Args map[string]any
}

// step is one model decision: either a tool call or the final answer.
type step struct {
	Call *toolCall
	Text string
}

// turn is one entry of the transcript the model sees.
type turn struct {
	Role      string // user | assistant | tool
	Text      string
	Call      *toolCall
	Result    string
	Principal string
	UserTurns int
}

type model interface {
	Step(ctx context.Context, transcript []turn) (step, error)
}

type tool func(ctx context.Context, args map[string]any) (string, error)

// fakeModel is the deterministic runner from README.md.
type fakeModel struct{}

func (fakeModel) Step(_ context.Context, transcript []turn) (step, error) {
	last := transcript[len(transcript)-1]
	var user turn
	for i := len(transcript) - 1; i >= 0; i-- {
		if transcript[i].Role == "user" {
			user = transcript[i]
			break
		}
	}
	text := strings.TrimSpace(user.Text)
	switch {
	case last.Role == "tool":
		var res map[string]any
		if err := json.Unmarshal([]byte(last.Result), &res); err != nil {
			return step{}, fmt.Errorf("tool result: %w", err)
		}
		return step{Text: fmt.Sprintf("%v is %v (%v replica); principal=%s; turn %d",
			res["service"], res["status"], res["replicas"], user.Principal, user.UserTurns)}, nil
	case strings.HasPrefix(text, "status of "):
		return step{Call: &toolCall{Name: "get_service_status", Args: map[string]any{"service": strings.TrimPrefix(text, "status of ")}}}, nil
	case strings.HasPrefix(text, "slow "):
		return step{Call: &toolCall{Name: "get_service_status", Args: map[string]any{"service": strings.TrimPrefix(text, "slow "), "slow": true}}}, nil
	default:
		return step{Text: fmt.Sprintf("turn %d; principal=%s", user.UserTurns, user.Principal)}, nil
	}
}

func fakeTools() map[string]tool {
	return map[string]tool{
		"get_service_status": func(ctx context.Context, args map[string]any) (string, error) {
			if slow, _ := args["slow"].(bool); slow {
				select {
				case <-time.After(20 * time.Second):
				case <-ctx.Done():
					return "", ctx.Err()
				}
			}
			out, _ := json.Marshal(map[string]any{"service": args["service"], "status": "degraded", "replicas": 1})
			return string(out), nil
		},
	}
}

// --- the A2A executor ---

type supportExecutor struct {
	store *pgStore
	model model
	tools map[string]tool
}

var _ a2asrv.AgentExecutor = (*supportExecutor)(nil)

func (e *supportExecutor) Execute(ctx context.Context, execCtx *a2asrv.ExecutorContext) iter.Seq2[a2a.Event, error] {
	return func(yield func(a2a.Event, error) bool) {
		principal := "-"
		if execCtx.User != nil && execCtx.User.Authenticated {
			principal = execCtx.User.Name
		}
		if execCtx.StoredTask == nil {
			if !yield(a2a.NewSubmittedTask(execCtx, execCtx.Message), nil) {
				return
			}
		}
		if !yield(a2a.NewStatusUpdateEvent(execCtx, a2a.TaskStateWorking, nil), nil) {
			return
		}

		transcript, err := e.restoreTranscript(ctx, execCtx, principal)
		if err != nil {
			yield(nil, err)
			return
		}

		// The SDK cancels this ctx only when the cancel lands on the same
		// replica (local_manager.go handleCancelWithConcurrentRun). A cancel
		// on the other replica is visible only as a state change in the
		// store, so the loop watches the store and cancels itself.
		loopCtx, stop := context.WithCancel(ctx)
		defer stop()
		go e.watchCancel(loopCtx, stop, execCtx.TaskID)

		answer, err := e.runLoop(loopCtx, transcript)
		if errors.Is(err, context.Canceled) {
			bg := context.WithoutCancel(ctx)
			if perr := e.store.patchMetadata(bg, execCtx.TaskID, "loop_stopped", true); perr != nil {
				log.Printf("loop_stopped not recorded for %s: %v", execCtx.TaskID, perr)
			}
			ev := a2a.NewStatusUpdateEvent(execCtx, a2a.TaskStateCanceled, nil)
			ev.Metadata = map[string]any{"loop_stopped": true}
			yield(ev, nil)
			return
		}
		if err != nil {
			yield(a2a.NewStatusUpdateEvent(execCtx, a2a.TaskStateFailed, a2a.NewMessageForTask(a2a.MessageRoleAgent, execCtx, a2a.NewTextPart(err.Error()))), nil)
			return
		}
		if !yield(a2a.NewArtifactEvent(execCtx, a2a.NewTextPart(answer)), nil) {
			return
		}
		yield(a2a.NewStatusUpdateEvent(execCtx, a2a.TaskStateCompleted, nil), nil)
	}
}

func (e *supportExecutor) Cancel(ctx context.Context, execCtx *a2asrv.ExecutorContext) iter.Seq2[a2a.Event, error] {
	return func(yield func(a2a.Event, error) bool) {
		yield(a2a.NewStatusUpdateEvent(execCtx, a2a.TaskStateCanceled, nil), nil)
	}
}

// restoreTranscript rebuilds the context's conversation from the task store
// (every task of this context owned by the caller) and counts user turns.
func (e *supportExecutor) restoreTranscript(ctx context.Context, execCtx *a2asrv.ExecutorContext, principal string) ([]turn, error) {
	list, err := e.store.List(ctx, &a2a.ListTasksRequest{ContextID: execCtx.ContextID, PageSize: 100})
	if err != nil {
		return nil, fmt.Errorf("restore transcript: %w", err)
	}
	var transcript []turn
	userTurns := 0
	for _, t := range list.Tasks {
		for _, m := range t.History {
			if m.ID == execCtx.Message.ID {
				continue
			}
			if m.Role == a2a.MessageRoleUser {
				userTurns++
			}
			transcript = append(transcript, turn{Role: strings.ToLower(strings.TrimPrefix(string(m.Role), "ROLE_")), Text: textOf(m)})
		}
	}
	userTurns++
	transcript = append(transcript, turn{Role: "user", Text: textOf(execCtx.Message), Principal: principal, UserTurns: userTurns})
	return transcript, nil
}

func (e *supportExecutor) runLoop(ctx context.Context, transcript []turn) (string, error) {
	for i := 0; i < 8; i++ {
		st, err := e.model.Step(ctx, transcript)
		if err != nil {
			return "", err
		}
		if st.Call == nil {
			return st.Text, nil
		}
		fn, ok := e.tools[st.Call.Name]
		if !ok {
			return "", fmt.Errorf("unknown tool %q", st.Call.Name)
		}
		res, err := fn(ctx, st.Call.Args)
		if err != nil {
			return "", err
		}
		transcript = append(transcript, turn{Role: "assistant", Call: st.Call}, turn{Role: "tool", Call: st.Call, Result: res})
	}
	return "", errors.New("tool loop did not converge")
}

func (e *supportExecutor) watchCancel(ctx context.Context, stop context.CancelFunc, id a2a.TaskID) {
	tick := time.NewTicker(250 * time.Millisecond)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			st, err := e.store.Get(ctx, id)
			if err == nil && st.Task.Status.State.Terminal() {
				stop()
				return
			}
		}
	}
}

func textOf(m *a2a.Message) string {
	if m == nil {
		return ""
	}
	var parts []string
	for _, p := range m.Parts {
		if t, ok := p.Content.(a2a.Text); ok {
			parts = append(parts, string(t))
		}
	}
	return strings.Join(parts, "\n")
}
