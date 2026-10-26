package g0_test

import (
	"crypto/ed25519"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"

	"mctl.ai/spikes/mcpgatewayg0/model"
)

func apiVerifier(e *env) model.Verifier {
	return model.Verifier{JWKS: e.jwks, Issuer: issuer, Audience: audAPI, Clock: e.clock, Skew: 5 * time.Second}
}

// T1
func TestT1_GatewayAcceptsGatewayAudienceAPIRefuses(t *testing.T) {
	e := newEnv(t)
	for _, pid := range []string{pidA, pidB} {
		tok := e.gwToken(pid)
		c, err := e.gw.Verifier.Verify(tok)
		if err != nil || c.Pid != pid {
			t.Fatalf("gateway refused %s: %v", pid, err)
		}
		if _, err := apiVerifier(e).Verify(tok); !errors.Is(err, model.ErrAudience) {
			t.Fatalf("api must refuse gateway-audience token, got %v", err)
		}
	}
}

// T2
func TestT2_RefusalReasonsAtGatewayAndAPI(t *testing.T) {
	e := newEnv(t)
	good := model.Claims{Iss: issuer, Sub: pidA, Pid: pidA, Iat: t0.Unix(), Exp: t0.Add(time.Hour).Unix()}
	with := func(f func(*model.Claims)) model.Claims { c := good; f(&c); return c }
	forge := func(h model.Header, c model.Claims, sign func([]byte) []byte) string {
		return model.Assemble(h, c, sign)
	}
	edSign := func(k model.Key) func([]byte) []byte {
		return func(in []byte) []byte { return ed25519.Sign(k.Private, in) }
	}
	hmacSign := func(in []byte) []byte { m := hmac.New(sha256.New, []byte("shared")); m.Write(in); return m.Sum(nil) }
	hdr := model.Header{Alg: "EdDSA", Kid: e.keyA.Kid, Typ: "at+jwt"}
	unknown := model.KeyFromSeed("fx-unknown", make([]byte, 32))
	rogue := model.KeyFromSeed(e.keyA.Kid, []byte("0123456789abcdef0123456789abcdef")) // right kid, wrong key

	aud := func(a string) func(*model.Claims) { return func(c *model.Claims) { c.Aud = model.Audience{a} } }
	cases := []struct {
		name   string
		mk     func(a string) string // builds the bad token addressed to resource a
		reason error
	}{
		{"wrong audience", func(a string) string { return forge(hdr, with(aud("https://other.example")), edSign(e.keyA)) }, model.ErrAudience},
		{"expired", func(a string) string {
			return forge(hdr, with(func(c *model.Claims) { aud(a)(c); c.Exp = t0.Add(-time.Hour).Unix() }), edSign(e.keyA))
		}, model.ErrExpired},
		{"bad signature", func(a string) string { return forge(hdr, with(aud(a)), edSign(rogue)) }, model.ErrSignature},
		{"unknown kid", func(a string) string {
			return forge(model.Header{Alg: "EdDSA", Kid: "fx-unknown", Typ: "at+jwt"}, with(aud(a)), edSign(unknown))
		}, model.ErrUnknownKid},
		{"alg none", func(a string) string {
			return forge(model.Header{Alg: "none", Kid: e.keyA.Kid}, with(aud(a)), func([]byte) []byte { return nil })
		}, model.ErrAlg},
		{"alg HS256", func(a string) string {
			return forge(model.Header{Alg: "HS256", Kid: e.keyA.Kid}, with(aud(a)), hmacSign)
		}, model.ErrAlg},
		{"sub/pid mismatch", func(a string) string {
			return forge(hdr, with(func(c *model.Claims) { aud(a)(c); c.Sub = pidB }), edSign(e.keyA))
		}, model.ErrSubPid},
		{"missing pid", func(a string) string {
			return forge(hdr, with(func(c *model.Claims) { aud(a)(c); c.Pid = "" }), edSign(e.keyA))
		}, model.ErrSubPid},
		{"wrong issuer", func(a string) string {
			return forge(hdr, with(func(c *model.Claims) { aud(a)(c); c.Iss = "https://evil.example" }), edSign(e.keyA))
		}, model.ErrIssuer},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := e.gw.Verifier.Verify(tc.mk(audGW)); !errors.Is(err, tc.reason) {
				t.Fatalf("gateway: want %v got %v", tc.reason, err)
			}
			if _, err := apiVerifier(e).Verify(tc.mk(audAPI)); !errors.Is(err, tc.reason) {
				t.Fatalf("api: want %v got %v", tc.reason, err)
			}
			bad := bearerOf(tc.mk(audGW))
			if _, err := e.gw.ListTools(bad); err != model.ErrUnauthorized {
				t.Fatalf("list: %v", err)
			}
			if _, err := e.gw.CallTool(bad, "api_list_services", nil); err != model.ErrUnauthorized {
				t.Fatalf("call: %v", err)
			}
		})
	}
}

// T3
func TestT3_ExchangeShape(t *testing.T) {
	e := newEnv(t)
	subj := e.gwToken(pidA)
	sc, _ := e.gw.Verifier.Verify(subj)
	out, err := e.xchg.Exchange(model.ExchangeRequest{GrantType: model.GrantTokenExchange, SubjectToken: subj, ActorToken: e.gw.ActorToken, Resource: audAPI})
	if err != nil {
		t.Fatal(err)
	}
	c, err := apiVerifier(e).Verify(out.String())
	if err != nil {
		t.Fatalf("api must accept exchanged token: %v", err)
	}
	if c.Sub != sc.Sub || c.Pid != pidA || c.Act == nil || c.Act.Sub != e.fx.GatewayPrincipal || !c.Aud.Contains(audAPI) {
		t.Fatalf("bad exchanged claims %+v", c)
	}
	if c.Exp > sc.Exp || c.Exp > e.clock.Now().Add(5*time.Minute).Unix() || c.Exp != e.clock.Now().Add(5*time.Minute).Unix() {
		t.Fatalf("exp not min(subject, now+max): %d vs %d", c.Exp, sc.Exp)
	}
	// subject expiring sooner than the ceiling bounds the issued token
	short := e.mint(model.Claims{Aud: model.Audience{audGW}, Sub: pidA, Pid: pidA, Scope: "tools"}, 90*time.Second)
	out, _ = e.xchg.Exchange(model.ExchangeRequest{GrantType: model.GrantTokenExchange, SubjectToken: short, ActorToken: e.gw.ActorToken, Resource: audAPI})
	c, _ = apiVerifier(e).Verify(out.String())
	if c.Exp != t0.Add(90*time.Second).Unix() {
		t.Fatalf("exp must follow short subject: %d", c.Exp)
	}
	if c.Scope != "tools" {
		t.Fatalf("scope %q", c.Scope)
	}
}

// T4
func TestT4_ExchangeRefusals(t *testing.T) {
	e := newEnv(t)
	subj := e.gwToken(pidA)
	userActor := e.mint(model.Claims{Aud: model.Audience{audToken}, Sub: pidB, Pid: pidB}, time.Hour)
	apiAud := e.mint(model.Claims{Aud: model.Audience{audAPI}, Sub: pidA, Pid: pidA}, time.Hour)
	expired := e.mint(model.Claims{Aud: model.Audience{audGW}, Sub: pidA, Pid: pidA, Iat: t0.Add(-2 * time.Hour).Unix(), Exp: t0.Add(-time.Hour).Unix()}, 0)
	base := model.ExchangeRequest{GrantType: model.GrantTokenExchange, SubjectToken: subj, ActorToken: e.gw.ActorToken, Resource: audAPI}
	cases := []struct {
		name string
		mod  func(r *model.ExchangeRequest)
		want error
	}{
		{"non-gateway actor", func(r *model.ExchangeRequest) { r.ActorToken = userActor }, model.ErrActor},
		{"subject not for gateway", func(r *model.ExchangeRequest) { r.SubjectToken = apiAud }, model.ErrSubjectAudience},
		{"disallowed resource", func(r *model.ExchangeRequest) { r.Resource = "https://coolify.example" }, model.ErrResource},
		{"expired subject", func(r *model.ExchangeRequest) { r.SubjectToken = expired }, model.ErrSubjectInvalid},
		{"wider scope", func(r *model.ExchangeRequest) { r.Scope = "tools admin" }, model.ErrScope},
		{"wrong grant", func(r *model.ExchangeRequest) { r.GrantType = "authorization_code" }, model.ErrGrantType},
	}
	for _, tc := range cases {
		r := base
		tc.mod(&r)
		if _, err := e.xchg.Exchange(r); !errors.Is(err, tc.want) {
			t.Errorf("%s: want %v got %v", tc.name, tc.want, err)
		}
	}
	// an exchanged token cannot be re-exchanged (its aud is not the gateway)
	again := e.exchange(pidA, audAPI)
	r := base
	r.SubjectToken = again
	if _, err := e.xchg.Exchange(r); !errors.Is(err, model.ErrSubjectAudience) {
		t.Errorf("re-exchange: %v", err)
	}
}

// T5
func TestT5_JWKSRotation(t *testing.T) {
	e := newEnv(t)
	tokA := e.gwToken(pidA) // signed by A
	tokB := model.Sign(e.keyB, model.Claims{Iss: issuer, Aud: model.Audience{audGW}, Sub: pidA, Pid: pidA, Iat: t0.Unix(), Exp: t0.Add(time.Hour).Unix()})
	for _, tok := range []string{tokA, tokB} {
		if _, err := e.gw.Verifier.Verify(tok); err != nil {
			t.Fatalf("both published keys must verify: %v", err)
		}
	}
	e.jwks.Remove(e.keyA.Kid)
	if _, err := e.gw.Verifier.Verify(tokA); !errors.Is(err, model.ErrUnknownKid) {
		t.Fatalf("removed kid must be refused: %v", err)
	}
	if _, err := e.gw.Verifier.Verify(tokB); err != nil {
		t.Fatalf("retained kid must verify: %v", err)
	}
	if !strings.Contains(string(e.jwks.Document()), `"crv":"Ed25519"`) || strings.Contains(string(e.jwks.Document()), hex.EncodeToString(e.keyB.Private.Seed())) {
		t.Fatal("jwks document must publish public keys only")
	}
	e.jwks.SetAvailable(false)
	if _, err := e.gw.Verifier.Verify(tokB); !errors.Is(err, model.ErrKeysUnavailable) {
		t.Fatalf("jwks outage must fail closed: %v", err)
	}
}

func listNames(t *testing.T, e *env, pid string) []string {
	t.Helper()
	r, err := e.gw.ListTools(bearerOf(e.gwToken(pid)))
	if err != nil {
		t.Fatal(err)
	}
	var n []string
	for _, x := range r.Tools {
		n = append(n, x.Name)
	}
	return n
}

// T6
func TestT6_DifferentCataloguesGolden(t *testing.T) {
	e := newEnv(t)
	wantA := []string{"api_list_services", "api_get_service_status", "api_deploy_service", "api_delete_service", "tg_send_message"}
	wantB := []string{"api_list_services", "api_get_service_status", "api_deploy_service", "tg_list_chats"}
	if got := listNames(t, e, pidA); !reflect.DeepEqual(got, wantA) {
		t.Errorf("A: %v", got)
	}
	if got := listNames(t, e, pidB); !reflect.DeepEqual(got, wantB) {
		t.Errorf("B: %v", got)
	}
}

// T7
func TestT7_HiddenEqualsUnknownAndInertGrants(t *testing.T) {
	e := newEnv(t)
	tokB := bearerOf(e.gwToken(pidB))
	_, hidden := e.gw.CallTool(tokB, "api_delete_service", map[string]any{"tenant": "tenant-blue"}) // B lacks the grant
	_, unknown := e.gw.CallTool(tokB, "api_no_such_tool", map[string]any{"tenant": "tenant-blue"})
	_, noServer := e.gw.CallTool(tokB, "nosuch_tool", nil)
	if hidden != model.ErrUnknownTool || unknown != model.ErrUnknownTool || noServer != model.ErrUnknownTool {
		t.Fatalf("hidden=%v unknown=%v noServer=%v", hidden, unknown, noServer)
	}
	// grant outside the ceiling (internal_debug) is inert for owner A even by name
	_, err := e.gw.CallTool(bearerOf(e.gwToken(pidA)), "api_internal_debug", map[string]any{"tenant": "tenant-red"})
	if err != model.ErrUnknownTool {
		t.Fatalf("grant outside ceiling must be inert: %v", err)
	}
	// grant inside the ceiling but unpublished downstream (tg unpublished_tool) is inert
	if _, err := e.gw.CallTool(tokB, "tg_unpublished_tool", nil); err != model.ErrUnknownTool {
		t.Fatalf("unpublished tool must be inert: %v", err)
	}
	for _, n := range listNames(t, e, pidB) {
		if strings.Contains(n, "unpublished") || strings.Contains(n, "internal_debug") {
			t.Fatalf("inert grant listed: %s", n)
		}
	}
}

// T8
func TestT8_CrossTenantRefusedAtGatewayAndDownstream(t *testing.T) {
	e := newEnv(t)
	tokA := bearerOf(e.gwToken(pidA))
	if _, err := e.gw.CallTool(tokA, "api_deploy_service", map[string]any{"tenant": "tenant-red"}); err != nil {
		t.Fatalf("owner in own tenant must work: %v", err)
	}
	if _, err := e.gw.CallTool(tokA, "api_deploy_service", map[string]any{"tenant": "tenant-blue"}); err != model.ErrUnknownTool {
		t.Fatalf("red-owner authority must not authorize blue mutation: %v", err)
	}
	// gateway check bypassed: valid exchanged token straight to the downstream
	ds := httptest.NewServer(e.api)
	defer ds.Close()
	post := func(tenant string) int {
		body := `{"method":"tools/call","params":{"name":"deploy_service","arguments":{"tenant":"` + tenant + `"}}}`
		req, _ := http.NewRequest("POST", ds.URL, strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+e.exchange(pidA, audAPI))
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		return resp.StatusCode
	}
	if c := post("tenant-blue"); c != http.StatusForbidden {
		t.Fatalf("downstream must independently refuse cross-tenant, got %d", c)
	}
	if c := post("tenant-red"); c != http.StatusOK {
		t.Fatalf("downstream owner-in-red must pass, got %d", c)
	}
}

// T9
func TestT9_EveryCallReevaluatesPolicy(t *testing.T) {
	e := newEnv(t)
	tok := bearerOf(e.gwToken(pidB))
	listNames(t, e, pidB)
	before := e.gw.PolicyEvals.Load()
	for i := 0; i < 3; i++ {
		if _, err := e.gw.CallTool(tok, "api_list_services", nil); err != nil {
			t.Fatal(err)
		}
	}
	if got := e.gw.PolicyEvals.Load() - before; got != 3 {
		t.Fatalf("want 3 evaluations for 3 calls, got %d", got)
	}
	// a removed grant bites the very next call although tools/list had shown the tool
	snap := e.fx.Snapshot()
	snap.Version++
	snap.Grants = nil
	e.cache.Offer(snap)
	if _, err := e.gw.CallTool(tok, "api_list_services", nil); err != model.ErrUnknownTool {
		t.Fatalf("list result must not act as permission: %v", err)
	}
}

// T10
func TestT10_BypassModel(t *testing.T) {
	e := newEnv(t)
	gwTok := e.gwToken(pidA)
	call := func(srv *model.Downstream, tok string) int {
		rr := httptest.NewRecorder()
		req := httptest.NewRequest("POST", "/", strings.NewReader(`{"method":"tools/list"}`))
		req.Header.Set("Authorization", "Bearer "+tok)
		srv.ServeHTTP(rr, req)
		return rr.Code
	}
	// state 1 (today, mctl-api@98bbf8c verifyJWT ignores aud): the gateway token is a valid API bearer
	lv := e.api.Verifier
	lv.LegacyIgnoreAudience = true
	legacy := &model.Downstream{Name: "api", Verifier: lv, Catalogue: e.api.Catalogue, Tools: e.api.Tools, Membership: e.api.Membership}
	if c := call(legacy, gwTok); c != http.StatusOK {
		t.Fatalf("legacy state should accept (documenting the hazard), got %d", c)
	}
	// state 2/3 (audience enforced)
	if c := call(e.api, gwTok); c != http.StatusUnauthorized {
		t.Fatalf("enforced downstream must refuse gateway token, got %d", c)
	}
	if c := call(e.api, e.exchange(pidA, audAPI)); c != http.StatusOK {
		t.Fatalf("exchanged token must be accepted, got %d", c)
	}
	// the gateway never forwards the caller bearer
	e.api.Received, e.tg.Received = nil, nil
	caller := e.gwToken(pidB)
	if _, err := e.gw.CallTool(bearerOf(caller), "api_list_services", nil); err != nil {
		t.Fatal(err)
	}
	if _, err := e.gw.ListTools(bearerOf(caller)); err != nil {
		t.Fatal(err)
	}
	seen := append(append([]string{}, e.api.Received...), e.tg.Received...)
	if len(seen) == 0 {
		t.Fatal("nothing reached downstream")
	}
	for _, h := range seen {
		if strings.Contains(h, caller) || !strings.HasPrefix(h, "Bearer ") {
			t.Fatalf("caller bearer or non-bearer header forwarded: %q", h)
		}
		c, err := model.Verifier{JWKS: e.jwks, Issuer: issuer, Clock: e.clock, Skew: 5 * time.Second, Audience: audAPI}.Verify(strings.TrimPrefix(h, "Bearer "))
		if err != nil {
			c, err = model.Verifier{JWKS: e.jwks, Issuer: issuer, Clock: e.clock, Skew: 5 * time.Second, Audience: audTG}.Verify(strings.TrimPrefix(h, "Bearer "))
		}
		if err != nil || c.Act == nil {
			t.Fatalf("forwarded token must be a downstream-audience exchanged token: %v", err)
		}
	}
}

// T11
func TestT11_RevocationBound(t *testing.T) {
	removed := func(e *env) model.Snapshot {
		s := e.fx.Snapshot()
		s.Version++
		s.Grants = s.Grants[:0:0] // all grants removed
		return s
	}
	tok := func(e *env) string { return bearerOf(e.gwToken(pidB)) }

	t.Run("source reachable: refused by the soft refresh", func(t *testing.T) {
		e := newEnv(t)
		if _, err := e.gw.CallTool(tok(e), "api_list_services", nil); err != nil {
			t.Fatal(err)
		}
		e.clock.Advance(10 * time.Second)
		e.source.snap = removed(e) // visible at the source now
		start := e.clock.Now()
		var refusedAfter time.Duration = -1
		for i := 0; i < 400 && refusedAfter < 0; i++ {
			if _, err := e.gw.CallTool(tok(e), "api_list_services", nil); err == model.ErrUnknownTool {
				refusedAfter = e.clock.Now().Sub(start)
			}
			e.clock.Advance(time.Second)
		}
		if refusedAfter < 0 || refusedAfter > e.cache.RMax() {
			t.Fatalf("refused after %v, R_max %v", refusedAfter, e.cache.RMax())
		}
		if refusedAfter > soft {
			t.Fatalf("with a reachable source the soft TTL bounds it: %v", refusedAfter)
		}
	})

	t.Run("source unreachable: cache serves until the hard TTL then fails closed", func(t *testing.T) {
		e := newEnv(t)
		e.gw.CallTool(tok(e), "api_list_services", nil)
		fetchedAt := e.clock.Now()
		e.source.snap = removed(e)
		e.source.down = true
		visible := e.clock.Now()
		var firstDenied time.Duration = -1
		for i := 0; i < 400 && firstDenied < 0; i++ {
			e.clock.Advance(time.Second)
			if _, err := e.gw.CallTool(tok(e), "api_list_services", nil); err != nil {
				if err != model.ErrPolicyDown {
					t.Fatalf("must be the retriable policy error, got %v", err)
				}
				firstDenied = e.clock.Now().Sub(visible)
			}
		}
		if firstDenied < 0 || firstDenied > e.cache.RMax() {
			t.Fatalf("denied after %v, R_max %v", firstDenied, e.cache.RMax())
		}
		if got := e.clock.Now().Sub(fetchedAt); got < hard {
			t.Fatalf("served past hard TTL: %v", got)
		}
	})

	t.Run("slow source (refresh timeout) is treated as failure", func(t *testing.T) {
		e := newEnv(t)
		e.gw.CallTool(tok(e), "api_list_services", nil)
		e.clock.Advance(hard)
		e.source.slow = true
		if _, err := e.gw.CallTool(tok(e), "api_list_services", nil); err != model.ErrPolicyDown {
			t.Fatalf("slow refresh past the timeout must fail closed, got %v", err)
		}
	})

	t.Run("newer version revokes immediately, older is ignored", func(t *testing.T) {
		e := newEnv(t)
		e.gw.CallTool(tok(e), "api_list_services", nil)
		older := e.fx.Snapshot()
		older.Version--
		older.Grants = nil
		e.cache.Offer(older)
		if _, err := e.gw.CallTool(tok(e), "api_list_services", nil); err != nil {
			t.Fatalf("older version must be ignored: %v", err)
		}
		e.cache.Offer(removed(e))
		if _, err := e.gw.CallTool(tok(e), "api_list_services", nil); err != model.ErrUnknownTool {
			t.Fatalf("newer version must apply immediately: %v", err)
		}
	})

	t.Run("observed newer version forces refetch, fail closed if unreachable", func(t *testing.T) {
		e := newEnv(t)
		e.gw.CallTool(tok(e), "api_list_services", nil)
		e.source.snap = removed(e)
		e.cache.Observe(e.source.snap.Version)
		if _, err := e.gw.CallTool(tok(e), "api_list_services", nil); err != model.ErrUnknownTool {
			t.Fatalf("observed newer version must refetch and apply: %v", err)
		}
		e2 := newEnv(t)
		e2.gw.CallTool(tok(e2), "api_list_services", nil)
		e2.source.down = true
		e2.cache.Observe(99)
		if _, err := e2.gw.CallTool(tok(e2), "api_list_services", nil); err != model.ErrPolicyDown {
			t.Fatalf("unreachable + newer version known must fail closed: %v", err)
		}
	})

	t.Run("observed newer version, source lagging, fails closed", func(t *testing.T) {
		e := newEnv(t)
		e.gw.CallTool(tok(e), "api_list_services", nil)
		e.cache.Observe(e.source.snap.Version + 1)
		if _, err := e.gw.CallTool(tok(e), "api_list_services", nil); err != model.ErrPolicyDown {
			t.Fatalf("source below observed version must fail closed: %v", err)
		}
	})

	t.Run("token lifetime does not extend a grant", func(t *testing.T) {
		e := newEnv(t)
		long := bearerOf(e.mint(model.Claims{Aud: model.Audience{audGW}, Sub: pidB, Pid: pidB, Scope: "tools"}, 10*time.Hour))
		e.gw.CallTool(long, "api_list_services", nil)
		e.source.snap = removed(e)
		e.clock.Advance(e.cache.RMax())
		if _, err := e.gw.CallTool(long, "api_list_services", nil); err != model.ErrUnknownTool {
			t.Fatalf("policy must bite at R_max regardless of token exp: %v", err)
		}
	})
}

// T12
func TestT12_FailureMatrix(t *testing.T) {
	tokOf := func(e *env) string { return bearerOf(e.gwToken(pidB)) }
	t.Run("policy unavailable, no cache", func(t *testing.T) {
		e := newEnv(t)
		e.source.down = true
		r, err := e.gw.ListTools(tokOf(e))
		if err != model.ErrPolicyDown || !err.(*model.RPCError).Retriable || len(r.Tools) != 0 {
			t.Fatalf("list: %v %+v", err, r)
		}
		if _, err := e.gw.CallTool(tokOf(e), "api_list_services", nil); err != model.ErrPolicyDown {
			t.Fatalf("call: %v", err)
		}
	})
	t.Run("cache past hard TTL", func(t *testing.T) {
		e := newEnv(t)
		listNames(t, e, pidB)
		e.source.down = true
		e.clock.Advance(hard + time.Second)
		if _, err := e.gw.ListTools(tokOf(e)); err != model.ErrPolicyDown {
			t.Fatalf("list: %v", err)
		}
		if _, err := e.gw.CallTool(tokOf(e), "api_list_services", nil); err != model.ErrPolicyDown {
			t.Fatalf("call: %v", err)
		}
	})
	t.Run("stale cache within hard TTL still serves (known policy)", func(t *testing.T) {
		e := newEnv(t)
		listNames(t, e, pidB)
		e.source.down = true
		e.clock.Advance(soft + time.Second)
		if _, err := e.gw.CallTool(tokOf(e), "api_list_services", nil); err != nil {
			t.Fatalf("within hard TTL the last known policy applies: %v", err)
		}
	})
	t.Run("exchange unavailable", func(t *testing.T) {
		e := newEnv(t)
		e.xchg.Fault = model.FaultUnavailable
		if _, err := e.gw.CallTool(tokOf(e), "api_list_services", nil); err != model.ErrCredentialDown {
			t.Fatalf("call: %v", err)
		}
		if _, err := e.gw.ListTools(tokOf(e)); err != model.ErrCredentialDown {
			t.Fatalf("list: %v", err)
		}
		if len(e.api.Received)+len(e.tg.Received) != 0 {
			t.Fatal("no fallback credential may reach a downstream")
		}
	})
	t.Run("exchange returns invalid token", func(t *testing.T) {
		e := newEnv(t)
		e.xchg.Fault = model.FaultInvalidToken
		if _, err := e.gw.CallTool(tokOf(e), "api_list_services", nil); err != model.ErrCredentialDown {
			t.Fatalf("call: %v", err)
		}
		if len(e.api.Received) != 0 {
			t.Fatal("invalid token must not be forwarded")
		}
	})
	t.Run("downstream unavailable on call", func(t *testing.T) {
		e := newEnv(t)
		e.api.Down = true
		_, err := e.gw.CallTool(tokOf(e), "api_list_services", nil)
		if err != model.ErrDownstreamUnavailable || !err.(*model.RPCError).Retriable {
			t.Fatalf("got %v", err)
		}
	})
	t.Run("downstream failure on list is reported, not hidden", func(t *testing.T) {
		e := newEnv(t)
		e.tg.Down = true
		r, err := e.gw.ListTools(tokOf(e))
		if err != nil || !reflect.DeepEqual(r.Meta.FailedServers, []string{"tg"}) || r.Meta.Complete || len(r.Tools) == 0 {
			t.Fatalf("%v %+v", err, r)
		}
		for _, x := range r.Tools {
			if strings.HasPrefix(x.Name, "tg_") {
				t.Fatal("failed server leaked tools")
			}
		}
	})
	t.Run("malformed catalogue", func(t *testing.T) {
		e := newEnv(t)
		e.tg.Catalogue = e.fx.MalformedTG
		r, err := e.gw.ListTools(tokOf(e))
		if err != nil || !reflect.DeepEqual(r.Meta.FailedServers, []string{"tg"}) || r.Meta.Complete {
			t.Fatalf("%v %+v", err, r)
		}
	})
	t.Run("key rotation: removed kid refused, JWKS outage refused", func(t *testing.T) {
		e := newEnv(t)
		tok := tokOf(e)
		e.jwks.SetAvailable(false)
		if _, err := e.gw.ListTools(tok); err != model.ErrUnauthorized {
			t.Fatalf("jwks down must fail closed: %v", err)
		}
		e.jwks.SetAvailable(true)
		e.jwks.Remove(e.keyA.Kid)
		if _, err := e.gw.CallTool(tok, "api_list_services", nil); err != model.ErrUnauthorized {
			t.Fatalf("removed kid: %v", err)
		}
	})
}

// T13
func TestT13_AuditCorrelationAndNoLeaks(t *testing.T) {
	e := newEnv(t)
	tokA := e.gwToken(pidA)
	e.sink.Secrets = append(e.sink.Secrets, "SECRET-BODY-MARKER", "RESULT-BODY-MARKER")
	res, err := e.gw.CallTool(bearerOf(tokA), "api_deploy_service", map[string]any{"tenant": "tenant-red", "note": "SECRET-BODY-MARKER"})
	if err != nil || !strings.Contains(string(res.Body), "RESULT-BODY-MARKER") {
		t.Fatalf("%v", err)
	}
	// exercise many other paths so every log branch runs
	e.gw.CallTool(bearerOf(tokA), "api_deploy_service", map[string]any{"tenant": "tenant-blue"})
	e.gw.CallTool("Bearer garbage", "api_list_services", nil)
	e.gw.ListTools(bearerOf(tokA))
	e.source.down = true
	e.clock.Advance(hard + time.Second)
	e.gw.CallTool(bearerOf(tokA), "api_list_services", nil)
	e.gw.ListTools(bearerOf(tokA))

	lines := e.sink.Lines()
	var first map[string]any
	n := 0
	for _, l := range lines {
		if strings.HasPrefix(l, "{") {
			n++
		}
	}
	if n != 4 {
		t.Fatalf("want exactly one audit record per tools/call (4), got %d in %v", n, lines)
	}
	rec := lines[0]
	first = map[string]any{}
	if err := jsonUnmarshal(rec, &first); err != nil {
		t.Fatal(err)
	}
	for _, f := range []string{"principal", "tenant", "actor", "server", "tool", "policy_version", "gateway_request_id", "downstream_audit_id", "decision", "status"} {
		if v, ok := first[f]; !ok || v == "" || v == nil {
			t.Errorf("audit field %q missing/empty: %v", f, first)
		}
	}
	if first["principal"] != pidA || first["actor"] != e.fx.GatewayPrincipal || first["decision"] != "allow" || first["policy_version"].(float64) != 7 {
		t.Errorf("unexpected record %v", first)
	}
	if !strings.HasPrefix(first["downstream_audit_id"].(string), "ds-api-") || e.api.EdgeIDs[len(e.api.EdgeIDs)-1] == "" {
		t.Errorf("correlation ids not propagated: %v %v", first, e.api.EdgeIDs)
	}
	if e.sink.Redactions != 0 {
		t.Fatalf("model emitted a secret and relied on redaction: %d", e.sink.Redactions)
	}
	secrets := append([]string{"SECRET-BODY-MARKER", "RESULT-BODY-MARKER"}, e.tokens...)
	for _, k := range e.fx.Keys {
		secrets = append(secrets, hex.EncodeToString(k.Private.Seed()), hex.EncodeToString(k.Private))
	}
	for _, tk := range e.tokens {
		secrets = append(secrets, model.Signature(tk))
	}
	for _, l := range lines {
		for _, s := range secrets {
			if s != "" && strings.Contains(l, s) {
				t.Fatalf("leak of %.12q in %q", s, l)
			}
		}
	}
}
