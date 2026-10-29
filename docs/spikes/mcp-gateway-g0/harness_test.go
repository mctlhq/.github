package g0_test

import (
	"encoding/json"
	"net/http/httptest"
	"testing"
	"time"

	"mctl.ai/spikes/mcpgatewayg0/model"
)

const (
	issuer   = "https://api.mctl.ai"
	audGW    = "https://mcp.mctl.ai"
	audAPI   = "https://api.mctl.ai"
	audTG    = "https://tg.mctl.ai"
	audToken = "https://api.mctl.ai/oauth/token"
	pidA     = "prn_fixture_a"
	pidB     = "prn_fixture_b"
	soft     = 30 * time.Second
	hard     = 120 * time.Second
	refresh  = 5 * time.Second
)

var t0 = time.Date(2026, 10, 25, 12, 0, 0, 0, time.UTC)

// fakeSource is the policy source (mctl-api decision endpoint stand-in).
type fakeSource struct {
	clock *model.FakeClock
	snap  model.Snapshot
	down  bool
	slow  bool // fetch consumes RefreshTimeout+1s of fake time, then succeeds
}

func (s *fakeSource) Fetch() (model.Snapshot, error) {
	if s.down {
		return model.Snapshot{}, model.ErrPolicyUnavailable
	}
	if s.slow {
		s.clock.Advance(refresh + time.Second)
	}
	return s.snap, nil
}

type env struct {
	t      *testing.T
	fx     *model.Fixtures
	clock  *model.FakeClock
	jwks   *model.JWKS
	keyA   model.Key
	keyB   model.Key
	source *fakeSource
	cache  *model.PolicyCache
	xchg   *model.Exchanger
	gw     *model.Gateway
	api    *model.Downstream
	tg     *model.Downstream
	sink   *model.Sink
	tokens []string // every credential minted, for leak scans
}

func newEnv(t *testing.T) *env {
	t.Helper()
	fx, err := model.LoadFixtures("fixtures")
	if err != nil {
		t.Fatal(err)
	}
	e := &env{t: t, fx: fx, clock: model.NewFakeClock(t0), keyA: fx.Keys[0], keyB: fx.Keys[1]}
	e.jwks = model.NewJWKS(e.keyA, e.keyB)
	base := model.Verifier{JWKS: e.jwks, Issuer: issuer, Clock: e.clock, Skew: 5 * time.Second}
	e.source = &fakeSource{clock: e.clock, snap: fx.Snapshot()}
	e.cache = &model.PolicyCache{Clock: e.clock, Source: e.source, SoftTTL: soft, HardTTL: hard, RefreshTimeout: refresh}
	e.xchg = &model.Exchanger{Clock: e.clock, Issuer: issuer, Signer: e.keyA,
		Subject: base.ForAudience(audGW), Actor: base.ForAudience(audToken),
		GatewayPrincipal: fx.GatewayPrincipal, AllowedResources: []string{audAPI, audTG}, MaxTTL: 5 * time.Minute}
	mkDown := func(name, aud string, cat []byte) *model.Downstream {
		d := &model.Downstream{Name: name, Verifier: base.ForAudience(aud), Catalogue: cat,
			Tools: fx.DownstreamTools[name], Membership: map[string]map[string]string{}}
		for pid, p := range fx.Principals { // downstream's own copy of membership
			d.Membership[pid] = p.Tenants
		}
		return d
	}
	e.api = mkDown("api", audAPI, fx.Catalogues["api"])
	e.tg = mkDown("tg", audTG, fx.Catalogues["tg"])
	sa, st := httptest.NewServer(e.api), httptest.NewServer(e.tg)
	t.Cleanup(sa.Close)
	t.Cleanup(st.Close)
	e.sink = &model.Sink{}
	actor := e.mint(model.Claims{Aud: model.Audience{audToken}, Sub: fx.GatewayPrincipal, Pid: fx.GatewayPrincipal}, 24*time.Hour)
	e.gw = &model.Gateway{Verifier: base.ForAudience(audGW), Policy: e.cache, Exchanger: e.xchg, ActorToken: actor,
		Servers: map[string]model.Server{"api": {URL: sa.URL, Audience: audAPI}, "tg": {URL: st.URL, Audience: audTG}},
		Sink:    e.sink, Actor: fx.GatewayPrincipal}
	return e
}

// mint signs claims with key A and records the credential for leak scans.
func (e *env) mint(c model.Claims, ttl time.Duration) string {
	c.Iss = issuer
	if c.Iat == 0 {
		c.Iat = e.clock.Now().Unix()
	}
	if c.Exp == 0 {
		c.Exp = e.clock.Now().Add(ttl).Unix()
	}
	tok := model.Sign(e.keyA, c)
	e.tokens = append(e.tokens, tok)
	return tok
}

func (e *env) gwToken(pid string) string {
	return e.mint(model.Claims{Aud: model.Audience{audGW}, Sub: pid, Pid: pid, Scope: "tools"}, time.Hour)
}

func bearerOf(tok string) string { return "Bearer " + tok }

func (e *env) exchange(pid, aud string) string {
	tok, err := e.xchg.Exchange(model.ExchangeRequest{GrantType: model.GrantTokenExchange,
		SubjectToken: e.gwToken(pid), ActorToken: e.gw.ActorToken, Resource: aud})
	if err != nil {
		e.t.Fatal(err)
	}
	e.tokens = append(e.tokens, tok.String())
	return tok.String()
}

func jsonUnmarshal(s string, v any) error { return json.Unmarshal([]byte(s), v) }
