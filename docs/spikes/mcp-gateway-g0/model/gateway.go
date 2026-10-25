package model

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"sync/atomic"
	"time"
)

// RPCError is the explicit error surface. A hidden tool and an unknown tool
// return the identical value so hidden tools are not enumerable.
type RPCError struct {
	Code      int
	Message   string
	Retriable bool
}

func (e *RPCError) Error() string { return e.Message }

var (
	ErrUnknownTool           = &RPCError{-32602, "unknown tool", false}
	ErrUnauthorized          = &RPCError{-32001, "unauthorized", false}
	ErrForbidden             = &RPCError{-32002, "forbidden", false}
	ErrPolicyDown            = &RPCError{-32003, "policy unavailable", true}
	ErrCredentialDown        = &RPCError{-32004, "downstream credential unavailable", true}
	ErrDownstreamUnavailable = &RPCError{-32005, "downstream unavailable", true}
)

type Server struct {
	URL      string
	Audience string
}

type Tool struct {
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`
}

type ListMeta struct {
	FailedServers []string `json:"failed_servers,omitempty"`
	Complete      bool     `json:"complete"`
}

type ListResult struct {
	Tools []Tool   `json:"tools"`
	Meta  ListMeta `json:"_meta"`
}

type CallResult struct {
	Body              []byte
	DownstreamAuditID string
}

type Gateway struct {
	Verifier   Verifier // aud = gateway resource
	Policy     *PolicyCache
	Exchanger  TokenExchanger
	ActorToken string // gateway service credential
	Servers    map[string]Server
	Sink       *Sink
	Actor      string // gateway service principal id
	HTTP       *http.Client

	PolicyEvals atomic.Int64 // incremented on every effective-policy evaluation
	seq         atomic.Int64
}

func (g *Gateway) client() *http.Client {
	if g.HTTP == nil {
		g.HTTP = &http.Client{Timeout: 2 * time.Second}
	}
	return g.HTTP
}

func (g *Gateway) serverNames() []string {
	var l []string
	for k := range g.Servers {
		l = append(l, k)
	}
	sort.Strings(l)
	return l
}

func (g *Gateway) mint(caller, server string) (ExchangedToken, error) {
	srv := g.Servers[server]
	tok, err := g.Exchanger.Exchange(ExchangeRequest{GrantType: GrantTokenExchange,
		SubjectToken: caller, ActorToken: g.ActorToken, Resource: srv.Audience})
	if err != nil {
		return ExchangedToken{}, ErrCredentialDown
	}
	// Do not forward anything we cannot verify as: downstream audience,
	// the caller's identity, acting as the gateway.
	want, _ := g.Verifier.Verify(caller)
	c, err := g.Verifier.ForAudience(srv.Audience).Verify(tok.String())
	if err != nil || c.Pid != want.Pid || c.Act == nil || c.Act.Sub != g.Actor {
		return ExchangedToken{}, ErrCredentialDown
	}
	return tok, nil
}

// forward only accepts an ExchangedToken; the caller bearer cannot reach it.
func (g *Gateway) forward(url string, tok ExchangedToken, reqID string, body any) (int, http.Header, []byte, error) {
	b, _ := json.Marshal(body)
	req, _ := http.NewRequest(http.MethodPost, url, bytes.NewReader(b))
	req.Header.Set("Authorization", "Bearer "+tok.String())
	req.Header.Set("X-MCTL-Edge-Route", "gateway")
	req.Header.Set("X-MCTL-Edge-Request-Id", reqID)
	resp, err := g.client().Do(req)
	if err != nil {
		return 0, nil, nil, err
	}
	defer resp.Body.Close()
	out, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	return resp.StatusCode, resp.Header, out, nil
}

func bearer(h string) string { return strings.TrimPrefix(h, "Bearer ") }

func (g *Gateway) ListTools(authHeader string) (ListResult, error) {
	caller := bearer(authHeader)
	claims, err := g.Verifier.Verify(caller)
	if err != nil {
		g.Sink.Log("tools/list refused: unauthorized")
		return ListResult{}, ErrUnauthorized
	}
	snap, err := g.Policy.Get()
	if err != nil {
		g.Sink.Log("tools/list failed: policy unavailable")
		return ListResult{}, ErrPolicyDown
	}
	reqID := fmt.Sprintf("gw-%06d", g.seq.Add(1))
	res := ListResult{Tools: []Tool{}}
	for _, name := range g.serverNames() {
		g.PolicyEvals.Add(1)
		eff := snap.Effective(claims.Pid, "", name)
		if len(eff) == 0 {
			continue // no grant on this server: not even contacted
		}
		tok, err := g.mint(caller, name)
		if err != nil {
			g.Sink.Log("tools/list failed: downstream credential unavailable server=" + name)
			return ListResult{}, err
		}
		tools, ok := g.fetchCatalogue(name, tok, reqID)
		if !ok {
			res.Meta.FailedServers = append(res.Meta.FailedServers, name)
			continue
		}
		for _, t := range tools {
			if eff[t.Name] { // ceiling ∩ grants ∩ published
				res.Tools = append(res.Tools, Tool{Name: name + "_" + t.Name, Description: t.Description})
			}
		}
	}
	res.Meta.Complete = len(res.Meta.FailedServers) == 0
	return res, nil
}

func (g *Gateway) fetchCatalogue(server string, tok ExchangedToken, reqID string) ([]Tool, bool) {
	code, _, body, err := g.forward(g.Servers[server].URL, tok, reqID, map[string]any{"method": "tools/list"})
	if err != nil || code != http.StatusOK {
		return nil, false
	}
	var cat struct {
		Tools []Tool `json:"tools"`
	}
	if json.Unmarshal(body, &cat) != nil || cat.Tools == nil {
		return nil, false // malformed => omitted and reported, never "empty ok"
	}
	for _, t := range cat.Tools {
		if t.Name == "" {
			return nil, false
		}
	}
	return cat.Tools, true
}

func (g *Gateway) CallTool(authHeader, name string, args map[string]any) (res CallResult, err error) {
	rec := Record{Actor: g.Actor, GatewayRequestID: fmt.Sprintf("gw-%06d", g.seq.Add(1)), Decision: "deny", Status: "refused"}
	defer func() {
		if err == nil {
			rec.Decision, rec.Status = "allow", "ok"
		} else if rpc := (*RPCError)(nil); errors.As(err, &rpc) && rpc.Retriable {
			rec.Decision, rec.Status = "unavailable", rpc.Message
		} else if rpc != nil {
			rec.Status = rpc.Message
		}
		g.Sink.Emit(rec)
	}()
	caller := bearer(authHeader)
	claims, verr := g.Verifier.Verify(caller)
	if verr != nil {
		return res, ErrUnauthorized
	}
	rec.Principal = claims.Pid
	server, tool, found := "", "", false
	for _, s := range g.serverNames() {
		if strings.HasPrefix(name, s+"_") {
			server, tool, found = s, strings.TrimPrefix(name, s+"_"), true
		}
	}
	if !found {
		return res, ErrUnknownTool
	}
	tenant, _ := args["tenant"].(string)
	rec.Tenant, rec.Server, rec.Tool = tenant, server, tool
	snap, perr := g.Policy.Get()
	if perr != nil {
		return res, ErrPolicyDown
	}
	rec.PolicyVersion = snap.Version
	g.PolicyEvals.Add(1) // every call re-evaluates; a prior tools/list is never consulted
	if !snap.Effective(claims.Pid, tenant, server)[tool] {
		return res, ErrUnknownTool
	}
	tok, merr := g.mint(caller, server)
	if merr != nil {
		return res, merr
	}
	code, hdr, body, ferr := g.forward(g.Servers[server].URL, tok, rec.GatewayRequestID,
		map[string]any{"method": "tools/call", "params": map[string]any{"name": tool, "arguments": args}})
	switch {
	case ferr != nil || code >= 500:
		return res, ErrDownstreamUnavailable
	case code == http.StatusNotFound:
		return res, ErrUnknownTool // granted but unpublished: inert
	case code == http.StatusForbidden || code == http.StatusUnauthorized:
		rec.DownstreamAuditID = hdr.Get("X-MCTL-Audit-Id")
		return res, ErrForbidden
	case code != http.StatusOK:
		return res, ErrDownstreamUnavailable
	}
	rec.DownstreamAuditID = hdr.Get("X-MCTL-Audit-Id")
	return CallResult{Body: body, DownstreamAuditID: rec.DownstreamAuditID}, nil
}
