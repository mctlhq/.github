package model

import (
	"errors"
	"fmt"
	"strings"
	"sync/atomic"
	"time"
)

const GrantTokenExchange = "urn:ietf:params:oauth:grant-type:token-exchange"

var (
	ErrGrantType       = errors.New("unsupported grant_type")
	ErrActor           = errors.New("actor is not the registered gateway principal")
	ErrSubjectAudience = errors.New("subject token not addressed to the gateway")
	ErrSubjectInvalid  = errors.New("subject token invalid")
	ErrResource        = errors.New("target resource not allowed")
	ErrScope           = errors.New("requested authority wider than subject token")
	ErrExchangeDown    = errors.New("token exchange unavailable")
)

// Fault injects exchange failure modes for the E5 matrix.
type Fault int

const (
	FaultNone Fault = iota
	FaultUnavailable
	FaultInvalidToken
)

type ExchangeRequest struct {
	GrantType    string
	SubjectToken string
	ActorToken   string
	Resource     string // RFC 8707 resource / audience of the issued token
	Scope        string // optional narrowing
}

// ExchangedToken is deliberately a distinct type: the gateway forwarder only
// accepts this, so a caller bearer cannot be passed downstream by mistake.
type ExchangedToken struct{ raw string }

func (t ExchangedToken) String() string { return t.raw }

type TokenExchanger interface {
	Exchange(ExchangeRequest) (ExchangedToken, error)
}

// Exchanger models the RFC 8693 grant at the mctl-api token endpoint.
type Exchanger struct {
	Clock            Clock
	Issuer           string
	Signer           Key
	Subject          Verifier // aud = gateway resource
	Actor            Verifier // aud = token endpoint
	GatewayPrincipal string
	AllowedResources []string
	MaxTTL           time.Duration
	Fault            Fault
	seq              atomic.Int64
}

func (e *Exchanger) Exchange(r ExchangeRequest) (ExchangedToken, error) {
	if e.Fault == FaultUnavailable {
		return ExchangedToken{}, ErrExchangeDown
	}
	if r.GrantType != GrantTokenExchange {
		return ExchangedToken{}, ErrGrantType
	}
	act, err := e.Actor.Verify(r.ActorToken)
	if err != nil || act.Pid != e.GatewayPrincipal {
		return ExchangedToken{}, ErrActor
	}
	sub, err := e.Subject.Verify(r.SubjectToken)
	if err != nil {
		if errors.Is(err, ErrAudience) {
			return ExchangedToken{}, ErrSubjectAudience
		}
		return ExchangedToken{}, fmt.Errorf("%w: %v", ErrSubjectInvalid, err)
	}
	ok := false
	for _, a := range e.AllowedResources {
		ok = ok || a == r.Resource
	}
	if !ok {
		return ExchangedToken{}, ErrResource
	}
	scope := sub.Scope
	if r.Scope != "" {
		have := map[string]bool{}
		for _, s := range strings.Fields(sub.Scope) {
			have[s] = true
		}
		for _, s := range strings.Fields(r.Scope) {
			if !have[s] {
				return ExchangedToken{}, ErrScope
			}
		}
		scope = r.Scope
	}
	now := e.Clock.Now()
	exp := now.Add(e.MaxTTL).Unix()
	if sub.Exp < exp {
		exp = sub.Exp
	}
	out := Claims{
		Iss: e.Issuer, Aud: Audience{r.Resource}, Sub: sub.Sub, Pid: sub.Pid,
		Iat: now.Unix(), Exp: exp, ClientID: sub.ClientID, Scope: scope,
		Jti: fmt.Sprintf("xchg-%d", e.seq.Add(1)), Act: &Actor{Sub: act.Pid},
	}
	if e.Fault == FaultInvalidToken {
		return ExchangedToken{raw: "garbage.token.value"}, nil
	}
	return ExchangedToken{raw: Sign(e.Signer, out)}, nil
}
