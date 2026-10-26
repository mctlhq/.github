package g0_test

import (
	"testing"

	"mctl.ai/spikes/mcpgatewayg0/model"
)

// Reference estimates on the in-process model only. NOT production latency.

func BenchmarkVerify(b *testing.B) {
	e := newEnv(&testing.T{})
	tok := e.gwToken(pidA)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, err := e.gw.Verifier.Verify(tok); err != nil {
			b.Fatal(err)
		}
	}
}

func BenchmarkExchange(b *testing.B) {
	e := newEnv(&testing.T{})
	r := model.ExchangeRequest{GrantType: model.GrantTokenExchange, SubjectToken: e.gwToken(pidA), ActorToken: e.gw.ActorToken, Resource: audAPI}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, err := e.xchg.Exchange(r); err != nil {
			b.Fatal(err)
		}
	}
}

func BenchmarkPolicyLookupHit(b *testing.B) {
	e := newEnv(&testing.T{})
	e.cache.Get()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		s, err := e.cache.Get()
		if err != nil {
			b.Fatal(err)
		}
		_ = s.Effective(pidA, "tenant-red", "api")
	}
}

func BenchmarkPolicyLookupMiss(b *testing.B) {
	e := newEnv(&testing.T{})
	e.cache.SoftTTL, e.cache.HardTTL = 0, 0 // every Get refetches from the in-process source
	for i := 0; i < b.N; i++ {
		s, err := e.cache.Get()
		if err != nil {
			b.Fatal(err)
		}
		_ = s.Effective(pidA, "tenant-red", "api")
	}
}

func BenchmarkCallRoundTrip(b *testing.B) {
	e := newEnv(&testing.T{})
	tok := bearerOf(e.gwToken(pidA))
	args := map[string]any{"tenant": "tenant-red"}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, err := e.gw.CallTool(tok, "api_deploy_service", args); err != nil {
			b.Fatal(err)
		}
	}
}
