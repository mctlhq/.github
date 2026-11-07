package automationcp

import (
	"fmt"
	"testing"
	"time"

	"mctl.ai/spikes/automationcp/model"
)

// BenchmarkFireIdempotent: replaying one occurrence must stay cheap and never grow state.
func BenchmarkFireIdempotent(b *testing.B) {
	e := newEnv(&testing.T{})
	d := e.create("k", model.Trigger{Kind: model.TrigAt, At: e.Clock.Now().Add(time.Hour)})
	e.Svc.Fire(d.ID, "o1")
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		e.Svc.Fire(d.ID, "o1")
	}
}

// BenchmarkScheduleStormTick: 1000 weekly schedules evaluated against one tick.
func BenchmarkScheduleStormTick(b *testing.B) {
	e := newEnv(&testing.T{})
	for i := 0; i < 1000; i++ {
		e.create(fmt.Sprint("k", i), model.Trigger{Kind: model.TrigSchedule, Weekday: time.Friday, Hour: 9, TZ: "UTC", Overlap: model.OverlapSkip})
	}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		e.Clock.Advance(time.Minute)
		e.Svc.Tick()
	}
}
