package model

import (
	"errors"
	"sort"
	"sync"
	"time"
)

var (
	ErrPolicyUnavailable = errors.New("policy unavailable")
	ErrVersionRegression = errors.New("policy source returned an older version")
)

type GrantSubject struct {
	Kind  string `json:"kind"` // role | principal | group
	Role  string `json:"role,omitempty"`
	ID    string `json:"id,omitempty"`
	Group string `json:"group,omitempty"`
}

type Grant struct {
	Subject GrantSubject `json:"subject"`
	Server  string       `json:"server"`
	Tools   []string     `json:"tools"` // exact names or "*" (whole server)
}

// Snapshot is one versioned policy document (grants + ceiling + membership).
type Snapshot struct {
	Version    int64                        `json:"version"`
	Ceiling    map[string][]string          `json:"ceiling"`
	Grants     []Grant                      `json:"grants"`
	Membership map[string]map[string]string `json:"membership"` // pid -> tenant -> role
	Groups     map[string][]string          `json:"groups"`     // pid -> platform groups
}

// Effective implements effective(P,T,S) = ceiling(S) ∩ ⋃{grants matching P,T,S}.
// An empty tenant means "any tenant the principal belongs to" (tools/list).
func (s Snapshot) Effective(pid, tenant, server string) map[string]bool {
	return s.effective(pid, tenant, server, true)
}

func (s Snapshot) effective(pid, tenant, server string, anyTenant bool) map[string]bool {
	ceiling := map[string]bool{}
	for _, t := range s.Ceiling[server] {
		ceiling[t] = true
	}
	roles := map[string]bool{}
	for t, r := range s.Membership[pid] {
		if (tenant == "" && anyTenant) || (tenant != "" && t == tenant) {
			roles[r] = true
		}
	}
	groups := map[string]bool{}
	for _, g := range s.Groups[pid] {
		groups[g] = true
	}
	out := map[string]bool{}
	for _, g := range s.Grants {
		if g.Server != server {
			continue
		}
		match := (g.Subject.Kind == "role" && roles[g.Subject.Role]) ||
			(g.Subject.Kind == "principal" && g.Subject.ID == pid) ||
			(g.Subject.Kind == "group" && groups[g.Subject.Group])
		if !match {
			continue
		}
		for _, t := range g.Tools {
			if t == "*" {
				for c := range ceiling {
					out[c] = true
				}
			} else if ceiling[t] { // not in the ceiling => inert
				out[t] = true
			}
		}
	}
	return out
}

func Names(set map[string]bool) []string {
	var l []string
	for k := range set {
		l = append(l, k)
	}
	sort.Strings(l)
	return l
}

type PolicySource interface{ Fetch() (Snapshot, error) }

// PolicyCache is the gateway's versioned, fail-closed policy cache.
type PolicyCache struct {
	Clock          Clock
	Source         PolicySource
	SoftTTL        time.Duration // try to refresh after this age; keep serving on failure
	HardTTL        time.Duration // never serve beyond this age
	RefreshTimeout time.Duration // a fetch slower than this counts as failed
	Fetches        int

	mu        sync.Mutex
	cur       *Snapshot
	fetchedAt time.Time
	stale     bool
	minVer    int64 // newest version observed; never serve below it
}

// RMax is the designed bound between a change becoming visible at the policy
// source and the gateway refusing the removed tool (report section D3).
func (c *PolicyCache) RMax() time.Duration { return c.HardTTL + c.RefreshTimeout }

func (c *PolicyCache) refresh() bool {
	start := c.Clock.Now()
	c.Fetches++
	snap, err := c.Source.Fetch()
	if err != nil || c.Clock.Now().Sub(start) > c.RefreshTimeout {
		return false
	}
	if snap.Version < c.minVer {
		return false // source still lags an observed newer version
	}
	if c.cur != nil && snap.Version < c.cur.Version {
		return false // regression: never trust an older version
	}
	c.cur, c.fetchedAt, c.stale = &snap, c.Clock.Now(), false
	return true
}

func (c *PolicyCache) Get() (Snapshot, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.cur != nil && !c.stale {
		age := c.Clock.Now().Sub(c.fetchedAt)
		if age < c.SoftTTL {
			return *c.cur, nil
		}
		if age < c.HardTTL {
			c.refresh() // failure keeps serving the cache until the hard TTL
			return *c.cur, nil
		}
	}
	if c.refresh() {
		return *c.cur, nil
	}
	return Snapshot{}, ErrPolicyUnavailable // unknown authorization => closed
}

// Offer delivers a snapshot out of band (push). Newer replaces immediately;
// older or equal is ignored.
func (c *PolicyCache) Offer(s Snapshot) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.cur == nil || s.Version > c.cur.Version {
		c.cur, c.fetchedAt, c.stale = &s, c.Clock.Now(), false
	}
}

// Observe records that a newer policy version exists (for example in a
// downstream or decision-endpoint response header): the cache must refetch
// before the next decision and fails closed if it cannot.
func (c *PolicyCache) Observe(version int64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.cur != nil && version > c.cur.Version {
		c.stale = true
		if version > c.minVer {
			c.minVer = version
		}
	}
}
