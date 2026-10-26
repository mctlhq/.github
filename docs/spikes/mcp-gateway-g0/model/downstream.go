package model

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"
)

// ToolSpec is the downstream's own authorization rule for a tool.
type ToolSpec struct {
	Roles       []string `json:"roles"`        // tenant roles allowed
	NeedsTenant bool     `json:"needs_tenant"` // arguments.tenant is mandatory
}

// Downstream is a reference MCP resource server (stands for api.mctl.ai). It
// does its own audience check and its own tenant authorization, independent of
// the gateway.
type Downstream struct {
	Name       string
	Verifier   Verifier
	Catalogue  []byte                       // raw tools/list result body
	Tools      map[string]ToolSpec          // tool -> rule
	Membership map[string]map[string]string // pid -> tenant -> role (its own copy)
	Down       bool

	mu       sync.Mutex
	Received []string // Authorization headers seen
	EdgeIDs  []string // X-MCTL-Edge-Request-Id values seen
	seq      int
}

type rpcReq struct {
	Method string `json:"method"`
	Params struct {
		Name      string         `json:"name"`
		Arguments map[string]any `json:"arguments"`
	} `json:"params"`
}

func (d *Downstream) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if d.Down {
		http.Error(w, "down", http.StatusServiceUnavailable)
		return
	}
	auth := r.Header.Get("Authorization")
	d.mu.Lock()
	d.Received = append(d.Received, auth)
	d.EdgeIDs = append(d.EdgeIDs, r.Header.Get("X-MCTL-Edge-Request-Id"))
	d.seq++
	auditID := fmt.Sprintf("ds-%s-%d", d.Name, d.seq)
	d.mu.Unlock()

	claims, err := d.Verifier.Verify(strings.TrimPrefix(auth, "Bearer "))
	if err != nil {
		http.Error(w, err.Error(), http.StatusUnauthorized)
		return
	}
	var req rpcReq
	if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req) != nil {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	w.Header().Set("X-MCTL-Audit-Id", auditID)
	switch req.Method {
	case "tools/list":
		w.Write(d.Catalogue)
	case "tools/call":
		spec, ok := d.Tools[req.Params.Name]
		if !ok {
			http.Error(w, "unknown tool", http.StatusNotFound)
			return
		}
		tenant, _ := req.Params.Arguments["tenant"].(string)
		if spec.NeedsTenant && tenant == "" {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		if tenant != "" {
			role := d.Membership[claims.Pid][tenant]
			allowed := false
			for _, ro := range spec.Roles {
				allowed = allowed || ro == role
			}
			if !allowed {
				http.Error(w, "forbidden", http.StatusForbidden)
				return
			}
		} else {
			// No tenant named: same union-of-roles rule as the gateway.
			allowed := false
			for _, role := range d.Membership[claims.Pid] {
				for _, ro := range spec.Roles {
					allowed = allowed || ro == role
				}
			}
			if !allowed {
				http.Error(w, "forbidden", http.StatusForbidden)
				return
			}
		}
		json.NewEncoder(w).Encode(map[string]any{"result": "RESULT-BODY-MARKER", "audit_id": auditID})
	default:
		http.Error(w, "unsupported", http.StatusBadRequest)
	}
}
