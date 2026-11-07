// Package model is a stdlib-only, fake-clock reference model for the MCTL
// automation control plane (mctlhq/.github#318). Results are [MODEL] evidence
// only; real Temporal behavior is measured separately in ../temporal-integration.
package model

import (
	"sync"
	"time"
)

// Clock is injected everywhere; the model never reads the wall clock.
type Clock interface{ Now() time.Time }

// FakeClock is a manually advanced clock.
type FakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func NewFakeClock(t time.Time) *FakeClock { return &FakeClock{t: t} }
func (c *FakeClock) Now() time.Time       { c.mu.Lock(); defer c.mu.Unlock(); return c.t }
func (c *FakeClock) Set(t time.Time)      { c.mu.Lock(); c.t = t; c.mu.Unlock() }
func (c *FakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	c.t = c.t.Add(d)
	c.mu.Unlock()
}
