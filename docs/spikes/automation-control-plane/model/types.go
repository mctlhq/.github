package model

import (
	"errors"
	"time"
)

// Closed error vocabulary. Every refusal in the contract is one of these.
var (
	ErrRawTemporal       = errors.New("raw_temporal_forbidden")
	ErrNotFound          = errors.New("not_found")
	ErrIdempotencyReused = errors.New("idempotency_key_reused")
	ErrVersionConflict   = errors.New("version_conflict")
	ErrSignatureInvalid  = errors.New("signature_invalid")
	ErrReplayWindow      = errors.New("replay_window")
	ErrDuplicate         = errors.New("duplicate_event")
	ErrFanoutCapped      = errors.New("fanout_capped")
	ErrBudgetExceeded    = errors.New("budget_exceeded")
	ErrUnsupportedEvent  = errors.New("unsupported_event_type")
	ErrOwnerNotMember    = errors.New("owner_not_member")
	ErrInstallRevoked    = errors.New("install_revoked")
	ErrIdentityUnlinked  = errors.New("identity_unlinked")
	ErrTemplateForbidden = errors.New("template_forbidden_for_actor")
	ErrCrash             = errors.New("injected_crash")
	ErrInvalid           = errors.New("invalid_argument")
)

// Trigger kinds and overlap policies (closed sets mapping to Temporal's).
const (
	TrigAt, TrigSchedule, TrigEvent, TrigDependency                    = "at", "schedule", "event", "dependency"
	OverlapSkip, OverlapBufferOne, OverlapCancelOther, OverlapAllowAll = "skip", "buffer_one", "cancel_other", "allow_all"
	StateEnabled, StatePaused, StateDeleted                            = "enabled", "paused", "deleted"
)

// Trigger is the closed trigger union of AutomationDefinition v0.
type Trigger struct {
	Kind string
	// at: offset-aware instant, or local wall time + zone + policies.
	At          time.Time
	LocalWall   string // "2006-01-02T15:04" in TZ, alternative to At
	TZ          string
	GapPolicy   string // "skip" (observed Temporal) | "shift_forward" (product policy)
	OverlapPick string // "later" (observed Temporal) | "first"
	// schedule: weekly calendar recurrence in TZ.
	Weekday time.Weekday
	Hour    int
	Minute  int
	CatchUp time.Duration
	Overlap string
	// event/dependency
	EventType     string
	SubjectPrefix string
	Await         bool // dependency template that waits for a signal
	FanoutCap     int
}

type Budget struct{ PerRunMinor, PerOwnerDayMinor int64 }

// Definition is AutomationDefinition v0 (a standing intent).
type Definition struct {
	ID, Tenant, Owner, Surface, Actor string
	Template                          string
	Trigger                           Trigger
	Install                           string
	InstallRev                        int
	Budget                            Budget
	State                             string
	Version                           int
	ExpiresAt                         time.Time
	MaxFires                          int
	Fires, Refusals                   int
	ReadOnly                          bool
	DeliveryChat                      string
	AuthRef                           string
}

// CreateRequest is the wire form. Raw Temporal fields exist only so they can be refused.
type CreateRequest struct {
	IdempotencyKey string
	Tenant, Owner  string
	Surface, Actor string
	Template       string
	Trigger        Trigger
	Install        string
	InstallRev     int
	Budget         Budget
	DeliveryChat   string
	TTL            time.Duration
	MaxFires       int
	AuthRef        string
	// forbidden raw Temporal parameters
	WorkflowType, TaskQueue, Namespace, Signal string
}

// Template is a registered, server-side mapping; users never name workflow types.
type Template struct {
	Name     string
	Mutating bool
	Support  bool // creatable by the support actor
}
