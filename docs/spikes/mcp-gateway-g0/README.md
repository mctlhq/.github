# MCP gateway G0 reference model

NON-NORMATIVE. This is a deterministic, stdlib-only Go model of the identity,
delegation and per-tool authorization boundary proposed in
`docs/plans/mcp-gateway.md` (mctlhq/.github#194), built for the G0 decision
spike mctlhq/.github#195. Everything it proves is `[REF]` (reference-model)
evidence; it is not proof that any production service behaves this way. The
report is `../mcp-gateway-g0.md`.

Run (no network, no third-party modules, no `go.sum`):

```sh
cd docs/spikes/mcp-gateway-g0
go vet ./... && go test ./...
go test -run xxx -bench . -benchmem ./...   # reference overhead estimates only
```

Layout: `model/` (token/JWKS, RFC 8693-shaped exchange, policy cache, gateway,
reference downstream, audit sink), `fixtures/` (principals, ceiling, grants,
catalogues incl. one malformed, keys), `model_test.go` (T1-T13),
`bench_test.go`.

`fixtures/keys.json` holds fixture-only Ed25519 seeds. They are public test
vectors, not secrets, and sign nothing outside these tests. The issuer string
`https://api.mctl.ai` appears only as a claim value; no real key material is
involved.
