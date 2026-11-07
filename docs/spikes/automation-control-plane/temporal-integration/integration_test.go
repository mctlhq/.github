// Real-server integration proof for mctlhq/.github#318 [TEMPORAL_DEV].
//
// Starts a pinned `temporal server start-dev` on loopback with in-memory
// state, registers an inert worker and drives real Schedules through the
// official Go SDK. No production endpoint or credential is used. A missing
// CLI, a server that does not start or a worker that cannot poll is a test
// FAILURE (never a skip).
package integration

import (
	"context"
	"fmt"
	"net"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"testing"
	"time"

	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
)

const (
	pinnedCLI = "1.10.0" // temporal CLI; bundles Server 1.32.2
	taskQueue = "automationcp-inert"
)

// InertWorkflow is synthetic: it touches nothing, optionally sleeps, returns "ok".
func InertWorkflow(ctx workflow.Context, sleep time.Duration) (string, error) {
	if sleep > 0 {
		if err := workflow.Sleep(ctx, sleep); err != nil {
			return "", err
		}
	}
	return "ok", nil
}

func freePorts(t *testing.T) (int, int) {
	l1, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("no free loopback port: %v", err)
	}
	defer l1.Close()
	l2, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("no free loopback port: %v", err)
	}
	defer l2.Close()
	return l1.Addr().(*net.TCPAddr).Port, l2.Addr().(*net.TCPAddr).Port
}

// startServer launches the dev server and returns a connected client.
func startServer(t *testing.T) client.Client {
	t.Helper()
	bin := os.Getenv("TEMPORAL_CLI")
	if bin == "" {
		t.Fatal("TEMPORAL_CLI is not set: the real-server proof must run (skip == failure)")
	}
	out, err := exec.Command(bin, "--version").CombinedOutput()
	if err != nil || !strings.Contains(string(out), "temporal version "+pinnedCLI) {
		t.Fatalf("temporal CLI is not the pinned %s: %q err=%v", pinnedCLI, out, err)
	}
	t.Logf("[TEMPORAL_DEV] cli: %s", strings.TrimSpace(string(out)))
	port, uiPort := freePorts(t)
	cmd := exec.Command(bin, "server", "start-dev", "--ip", "127.0.0.1",
		"--port", fmt.Sprint(port), "--ui-port", fmt.Sprint(uiPort), "--headless",
		"--namespace", "automationcp-test", "--log-level", "error")
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		t.Fatalf("cannot start dev server: %v", err)
	}
	t.Cleanup(func() {
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		_ = cmd.Wait()
	})
	addr := fmt.Sprintf("127.0.0.1:%d", port)
	var c client.Client
	deadline := time.Now().Add(60 * time.Second)
	for {
		c, err = client.Dial(client.Options{HostPort: addr, Namespace: "automationcp-test"})
		if err == nil {
			hctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			_, err = c.CheckHealth(hctx, nil)
			cancel()
			if err == nil {
				break
			}
			c.Close()
		}
		if time.Now().After(deadline) {
			t.Fatalf("dev server did not become ready: %v", err)
		}
		time.Sleep(500 * time.Millisecond)
	}
	t.Cleanup(c.Close)
	return c
}

func startWorker(t *testing.T, c client.Client) {
	t.Helper()
	w := worker.New(c, taskQueue, worker.Options{})
	w.RegisterWorkflow(InertWorkflow)
	if err := w.Start(); err != nil {
		t.Fatalf("worker did not start: %v", err)
	}
	t.Cleanup(w.Stop)
}

func TestTemporalDevServer(t *testing.T) {
	c := startServer(t)
	startWorker(t, c)
	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Second)
	defer cancel()
	sc := c.ScheduleClient()

	t.Run("TestTemporalDevServerScheduleExecutes", func(t *testing.T) {
		const sid = "au_test_exec"
		h, err := sc.Create(ctx, client.ScheduleOptions{
			ID:   sid,
			Spec: client.ScheduleSpec{Intervals: []client.ScheduleIntervalSpec{{Every: time.Second}}},
			Action: &client.ScheduleWorkflowAction{
				ID: "wf-" + sid, Workflow: InertWorkflow, Args: []any{time.Duration(0)}, TaskQueue: taskQueue,
			},
			Overlap:          enumspb.SCHEDULE_OVERLAP_POLICY_SKIP,
			RemainingActions: 1, // models the `at` one-shot
		})
		if err != nil {
			t.Fatalf("create schedule: %v", err)
		}
		t.Cleanup(func() { _ = h.Delete(context.Background()) })
		var desc *client.ScheduleDescription
		var wfID, runID string
		for i := 0; i < 40; i++ {
			desc, err = h.Describe(ctx)
			if err != nil {
				t.Fatalf("describe: %v", err)
			}
			if len(desc.Info.RecentActions) > 0 {
				r := desc.Info.RecentActions[0].StartWorkflowResult
				wfID, runID = r.WorkflowID, r.FirstExecutionRunID
				break
			}
			time.Sleep(500 * time.Millisecond)
		}
		if wfID == "" {
			t.Fatal("schedule never fired")
		}
		var res string
		if err := c.GetWorkflow(ctx, wfID, runID).Get(ctx, &res); err != nil || res != "ok" {
			t.Fatalf("workflow result=%q err=%v", res, err)
		}
		// one-shot: wait past several intervals and assert exactly one action.
		time.Sleep(3 * time.Second)
		desc, _ = h.Describe(ctx)
		if desc.Info.NumActions != 1 {
			t.Fatalf("one-shot fired %d times, want 1", desc.Info.NumActions)
		}
		t.Logf("[TEMPORAL_DEV] schedule_id=%s workflow_id=%s run_id=%s result=%s num_actions=%d", sid, wfID, runID, res, desc.Info.NumActions)
	})

	t.Run("TestTemporalDevServerOverlapSkipAndPause", func(t *testing.T) {
		const sid = "au_test_overlap"
		h, err := sc.Create(ctx, client.ScheduleOptions{
			ID:   sid,
			Spec: client.ScheduleSpec{Intervals: []client.ScheduleIntervalSpec{{Every: time.Second}}},
			Action: &client.ScheduleWorkflowAction{
				ID: "wf-" + sid, Workflow: InertWorkflow, Args: []any{6 * time.Second}, TaskQueue: taskQueue,
			},
			Overlap: enumspb.SCHEDULE_OVERLAP_POLICY_SKIP,
		})
		if err != nil {
			t.Fatalf("create: %v", err)
		}
		t.Cleanup(func() { _ = h.Delete(context.Background()) })
		var d *client.ScheduleDescription
		for i := 0; i < 40; i++ {
			time.Sleep(500 * time.Millisecond)
			if d, err = h.Describe(ctx); err != nil {
				t.Fatal(err)
			}
			if d.Info.NumActionsSkippedOverlap > 0 {
				break
			}
		}
		if d.Info.NumActions != 1 || d.Info.NumActionsSkippedOverlap == 0 {
			t.Fatalf("overlap=skip: actions=%d skipped=%d", d.Info.NumActions, d.Info.NumActionsSkippedOverlap)
		}
		if err := h.Pause(ctx, client.SchedulePauseOptions{Note: "spike"}); err != nil {
			t.Fatal(err)
		}
		d, _ = h.Describe(ctx)
		if !d.Schedule.State.Paused {
			t.Fatal("not paused")
		}
		before := d.Info.NumActions
		time.Sleep(8 * time.Second)
		d, _ = h.Describe(ctx)
		if d.Info.NumActions != before {
			t.Fatalf("paused schedule fired: %d -> %d", before, d.Info.NumActions)
		}
		t.Logf("[TEMPORAL_DEV] schedule_id=%s actions=%d skipped_overlap=%d paused_no_fire=true", sid, d.Info.NumActions, d.Info.NumActionsSkippedOverlap)
	})

	t.Run("TestTemporalDevServerDSTObserved", func(t *testing.T) {
		// Europe/Berlin: 2027-03-28 02:30 does not exist (gap); 2027-10-31 02:30 occurs twice.
		for _, tc := range []struct{ name, month, day string }{{"gap", "3", "28"}, {"overlap", "10", "31"}} {
			month, day := atoi(tc.month), atoi(tc.day)
			sid := "au_test_dst_" + tc.name
			h, err := sc.Create(ctx, client.ScheduleOptions{
				ID: sid,
				Spec: client.ScheduleSpec{
					TimeZoneName: "Europe/Berlin",
					Calendars: []client.ScheduleCalendarSpec{{
						Year: []client.ScheduleRange{{Start: 2027}}, Month: []client.ScheduleRange{{Start: month}},
						DayOfMonth: []client.ScheduleRange{{Start: day}}, Hour: []client.ScheduleRange{{Start: 2}},
						Minute: []client.ScheduleRange{{Start: 30}},
					}},
				},
				Action: &client.ScheduleWorkflowAction{ID: "wf-" + sid, Workflow: InertWorkflow, Args: []any{time.Duration(0)}, TaskQueue: taskQueue},
			})
			if err != nil {
				t.Fatalf("create %s: %v", sid, err)
			}
			t.Cleanup(func() { _ = h.Delete(context.Background()) })
			d, err := h.Describe(ctx)
			if err != nil {
				t.Fatal(err)
			}
			var got []string
			for _, ts := range d.Info.NextActionTimes {
				got = append(got, ts.UTC().Format(time.RFC3339))
			}
			t.Logf("[TEMPORAL_DEV] dst_%s europe/berlin 02:30 next_action_times_utc=%v", tc.name, got)
			if len(got) > 1 {
				t.Fatalf("%s: expected at most one computed next action, got %v", tc.name, got)
			}
		}
	})
}

func atoi(s string) int { n := 0; fmt.Sscan(s, &n); return n }
