package model

import (
	"encoding/json"
	"strings"
	"sync"
)

// Record is the per-call correlation record. It has no field that can carry a
// credential or a request/result body.
type Record struct {
	Principal         string `json:"principal"`
	Tenant            string `json:"tenant"`
	Actor             string `json:"actor"`
	Server            string `json:"server"`
	Tool              string `json:"tool"`
	PolicyVersion     int64  `json:"policy_version"`
	GatewayRequestID  string `json:"gateway_request_id"`
	DownstreamAuditID string `json:"downstream_audit_id"`
	Decision          string `json:"decision"` // allow | deny | unavailable | error
	Status            string `json:"status"`
}

// Sink collects audit and log lines. Secrets lists strings that must never be
// emitted; any hit is redacted AND counted, and tests assert the count is 0,
// so the model never relies on redaction.
type Sink struct {
	mu         sync.Mutex
	lines      []string
	Secrets    []string
	Redactions int
}

func (s *Sink) add(line string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, sec := range s.Secrets {
		if sec != "" && strings.Contains(line, sec) {
			s.Redactions++
			line = strings.ReplaceAll(line, sec, "[REDACTED]")
		}
	}
	s.lines = append(s.lines, line)
}

func (s *Sink) Emit(r Record)  { b, _ := json.Marshal(r); s.add(string(b)) }
func (s *Sink) Log(msg string) { s.add(msg) }
func (s *Sink) Lines() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.lines...)
}
