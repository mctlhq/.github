package model

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strings"
	"time"
)

// Envelope mirrors events/schemas/event-envelope.v1.schema.json (the fields
// the router uses). Tenant binding is hydrated server-side, never trusted
// from the payload: it comes from the verified producer's binding table.
type Envelope struct {
	SpecVersion   string            `json:"specversion"`
	ID            string            `json:"id"`
	Type          string            `json:"type"`
	Source        string            `json:"source"`
	OccurredAt    string            `json:"occurred_at"`
	CorrelationID string            `json:"correlation_id"`
	Subject       map[string]string `json:"subject"`
}

// Sign is the producer-side HMAC (synthetic test keys only).
func Sign(key []byte, ts time.Time, body []byte) string {
	m := hmac.New(sha256.New, key)
	m.Write([]byte(ts.UTC().Format(time.RFC3339)))
	m.Write([]byte("."))
	m.Write(body)
	return "sha256=" + hex.EncodeToString(m.Sum(nil))
}

// Verifier authenticates at the ingress producer: signature then replay window.
type Verifier struct {
	Key    []byte
	Window time.Duration
	Clock  Clock
}

func (v *Verifier) Verify(ts time.Time, sig string, body []byte) (*Envelope, error) {
	if !hmac.Equal([]byte(sig), []byte(Sign(v.Key, ts, body))) {
		return nil, ErrSignatureInvalid
	}
	d := v.Clock.Now().Sub(ts)
	if d < -v.Window || d > v.Window {
		return nil, ErrReplayWindow
	}
	var e Envelope
	if err := json.Unmarshal(body, &e); err != nil || e.SpecVersion != "mctl.events/v1" || e.ID == "" {
		return nil, ErrInvalid
	}
	return &e, nil
}

// supportedNow is the closed set of event types with a verified producer.
// github.check_run / github.workflow_run have none yet (events/README.md).
func supportedNow(t string) bool {
	return strings.HasPrefix(t, "telegram.message.") ||
		strings.HasPrefix(t, "github.pull_request.") || strings.HasPrefix(t, "github.pull_request_review.")
}
