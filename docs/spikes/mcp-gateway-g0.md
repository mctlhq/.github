# MCP gateway G0: identity, per-tool policy and tenant isolation

Spike report for mctlhq/.github#195 (carrier DevLoop; refs only, not closing).
Accepted design under test: `docs/plans/mcp-gateway.md` (mctlhq/.github#194,
D1-D5 accepted 2026-10-17). Reference model: `mcp-gateway-g0/` (run
`go vet ./... && go test ./...` there).

## Non-claims and evidence tags

Every statement carries one tag. `[REF]` is reference-model or fixture
evidence from `mcp-gateway-g0/` and is never production proof. `[CODE]` is
read-only inspection of pinned production code. `[DOC]` is issue or plan text.
`unknown (not observed)` means this spike did not see it and does not assume
it. No production change was made and no repository other than
`mctlhq/.github` was written.

## Evidence pins (read 2026-10-25)

| Repository | Pin (40-hex) | Used for |
| --- | --- | --- |
| mctlhq/.github | `0c01961e48963f85866e0309060fd46274abf583` | plan, spike patterns, roadmap schema |
| mctlhq/mctl-api | `98bbf8c61006f1d70762c1ae2a9d3747c67ef241` | every `[CODE]` hook below. `main` is now `58867673ff16a9edbcdd411b9b0f113a3042c6fc` (9 commits ahead); of the hook files only `internal/api/router.go` changed, not re-read at the new head: delta unknown (not observed) |
| mctlhq/mctl-gitops | `d1661f7100608af22c29dbda02a0a367c4d3218e` | services listing, `mcp.mctl.ai` callback in `services/labs/mctl-telegram/values.yaml:151`. `main` is now `b47ac192b882b75259790de37c3dd46b4e9a7831`, not re-read |
| mctlhq/mctl-agents | `b0f943de5e50febd962ff46ebfe75212e6292204` (main at read date) | issue state only; no code read |
| mark3labs/mcp-go | `v1.0.0` (`51930cffc30e4708c60e9763a23ad0ed6ad06387`), the version pinned in `mctl-api` `go.mod`; latest observed `v1.3.0` (`e1bd0abdfa840bc3791fb963f2bbb9e7b809edd2`, 2026-10-14) | E6 |
| modelcontextprotocol/go-sdk | `v1.8.0` (`3f3b699b2b67e1ed033a63d6651671dab53c2d32`, 2026-09-04), latest observed | E6 |

Not observed: mctl-api's deployed `TENANT_AUTHZ_MODE` and access-token TTL
(`mctl-api` is not a catalogue service in `mctl_list_services(admins)`; TTL
default 1h is a code default, `internal/auth/oauth_server.go`, not deployed
config); the Cloudflare Portal configuration (`infrastructure/cloudflare/portal/`
is outside the gitops checkout read); downstream token verifiers (coolify,
tg, seerrsense, projects, alice): unknown (not observed).

## D1. Current-state and duplicate map

| Item | State | Owns | This spike |
| --- | --- | --- | --- |
| .github#194 `[DOC]` | closed (design merged) | accepted gateway design D1-D5, G0-G5 | the object under test; proposes revisions, does not edit it |
| .github#184 `[DOC]` | closed | Agent Bricks / Unity Gateway auth-context findings | input only, nothing duplicated |
| .github#35 `[DOC]` | open | Cloudflare Portal PoC (`enterprise-mcp` epic, gateway phase paused) | the incumbent being compared against |
| mctl-agents#242 `[DOC]` | closed | agent-side capability discovery/narrowing semantics | reused: server-qualified names, "discovery narrows, never grants" |
| mctl-api#377 `[DOC][CODE]` | closed | principal-backed tenant authorization | completed baseline, used as is (`PrincipalAuthz.Apply`, `authorizeSubject`) |
| mctl-api#488 `[DOC]` | open | caller-aware filtering of the native `api.mctl.ai/mcp` catalogue | not replaced; gateway consumes the downstream surface |
| mctl-api#747 `[DOC]` | open | agent delegated-subject precedent (`X-MCTL-On-Behalf-Of`) | actor/subject precedent only, not the exchange contract |

## D2. Identity and token contract

Caller token (proposed, modelled): JWS `alg=EdDSA`, `kid`, `typ=at+jwt`;
claims `iss=https://api.mctl.ai`, `aud=https://mcp.mctl.ai`, `sub`, `pid`,
`iat`, `exp`, `scope`. Verified offline against a JWKS (`model/token.go`,
`Verifier`). Rules: `alg` is pinned to `EdDSA` (so `none` and `HS256` are
refused before any key lookup), `kid` must be published, `iss` exact, `aud`
must contain the resource identifier exactly, bounded skew, and `sub`/`pid`
consistency as in `ValidateJWT` principal mode. A JWKS outage fails closed.

Exchange (RFC 8693-shaped, `model/exchange.go`): `grant_type=urn:ietf:params:oauth:grant-type:token-exchange`,
`subject_token` = caller token, `actor_token` = gateway service credential,
`resource` = downstream audience. Issued token: same `sub`/`pid`,
`act.sub` = gateway principal, `aud` = target, `exp = min(subject.exp,
now + MaxTTL)`, scope no wider. The gateway forwards only the `ExchangedToken`
type and re-verifies the result (audience, caller `pid`, `act`) before use.

Results `[REF]`: T1 (gateway accepts, API refuses same token), T2 (nine
refusal reasons at gateway and API, plus gateway entry points), T3 (claim
shape and `exp` bound), T4 (non-gateway actor, subject not for gateway,
disallowed resource, expired subject, wider scope, wrong grant, re-exchange of
an exchanged token), T5 (rotation: retained key accepted, removed `kid`
refused; JWKS document exposes public keys only).

Production hooks `[CODE]` at `mctl-api@98bbf8c`:

| Change | Hook |
| --- | --- |
| add `aud`, `kid`, `act` (and `scope`) | `internal/auth/oauth_server.go:jwtPayload` (today `iss,sub,pid,groups,iat,exp`, no `aud`) |
| asymmetric signing behind a flag, dual-accept HS256 during drain | `signJWT` (constant HS256 header, `JWTSecret` HMAC), `verifyJWT` (checks HMAC, `iss`, `exp` only), `IssueJWT`, `ValidateJWT` |
| enforce `aud=https://api.mctl.ai` with a grace rule for aud-less tokens until the 1h TTL drains | `verifyJWT` or `internal/auth/provider_localoauth.go:localOAuthProvider.Verify` (`Claims` routes by issuer only) |
| accept RFC 8707 `resource` on code/refresh grants, add token-exchange grant for the gateway principal | `internal/api/oauth_handlers.go:handleOAuthToken` (only `authorization_code`, `refresh_token`; otherwise `unsupported_grant_type`; `resource` ignored) |
| publish `jwks_uri` | `handleOAuthMeta`; no JWKS route exists in `internal/api/router.go` |
| per-resource metadata | `handleProtectedResourceMeta`, `.well-known/oauth-protected-resource[/mcp]` routes in `router.go` |

Finding `[CODE]`: because `verifyJWT` ignores `aud`, a gateway-audience token
from the same issuer would be a valid API bearer today (modelled in T10 with
`LegacyIgnoreAudience`). Ordering constraint: audience validation at
`api.mctl.ai` must be deployed before any `aud=https://mcp.mctl.ai` token is
minted. Contract choice recommended: accept RFC 8707 `resource` on the
authorization-code and refresh grants and bind the token audience to it
(both variants are expressible in the model; the client-registration-inferred
variant is not modelled). Signing algorithm: recommend EdDSA (modelled,
stdlib in Go); MCP client and downstream library support (for example
coolify's verifier) is unknown (not observed), so ES256 remains the fallback
if a consumer cannot verify EdDSA.

Independently useful: yes. Asymmetric signing + JWKS + `kid` + audience
validation lets any service verify mctl tokens without the shared HMAC
secret and closes the same-issuer substitution hazard whether or not the
gateway is ever built.

## D3. Policy model

`effective(P, T, S) = ceiling(S) ∩ ⋃ {grants matching P (role in T, principal, group) on S}`;
tools outside the ceiling or unpublished by the downstream are inert (T7).
`tools/list` uses the union over the principal's tenants; `tools/call`
evaluates with the call's tenant and re-evaluates every time (T9, counter on
policy evaluations). Hidden and unknown tools return the identical error (T7).
Golden catalogues (T6) `[REF]`: A = `api_list_services`, `api_get_service_status`,
`api_deploy_service`, `api_delete_service`, `tg_send_message`; B =
`api_list_services`, `api_get_service_status`, `api_deploy_service`,
`tg_list_chats`.

Cross-tenant (T8) `[REF]`: A (owner tenant-red, viewer tenant-blue) deploys in
red, but a blue mutation is refused at the gateway; with the gateway
bypassed, the reference downstream refuses it on its own membership copy.
Authoritative production check `[CODE]`: `internal/auth/principal_authz.go:PrincipalAuthz.Apply`,
`internal/api/tenant_authz_principal.go:authorizeSubject` (503 when undecidable),
reached by each MCP tool handler's REST re-check with the caller's bearer;
`internal/mcp/owner_tools.go:ownerOnlyCallMiddleware` is the precedent for
separating "decided no" from "could not verify". Whether every other
downstream's tools take a tenant argument in a form the gateway can read:
unknown (not observed).

Revocation bound. Cache: `version`, `fetched_at`, `soft_ttl` (refresh, keep
serving on failure), `hard_ttl` (never serve beyond), `refresh_timeout` (a
slower fetch counts as failed). A newer version replaces the snapshot at once;
an older one is ignored; an observed newer version forces a refetch and fails
closed if it cannot. Derivation: a change becomes visible at the source at
`t_v`; the cache's last successful fetch was at `t_f <= t_v`; no decision uses
a snapshot older than `hard_ttl`, so no call arriving after `t_f + hard_ttl <=
t_v + hard_ttl` is decided from the old policy; a refresh started at that
instant resolves (new policy or fail-closed) within `refresh_timeout`. Hence
`R_max = hard_ttl + refresh_timeout` (the same instant bound for decisions is
`hard_ttl`; `R_max` is kept as the conservative figure including the
refresh). With a reachable source the removal lands by the next soft refresh
(`soft_ttl`). Token lifetime is irrelevant to tool grants because policy is
checked per call (T11 last case). T11 `[REF]` exercises both paths under a
fake clock with soft 30 s, hard 120 s, timeout 5 s (R_max 125 s), the
slow-source, newer-version, older-version and observed-version cases. These
are test parameters, not recommended or deployed values: production values
unknown (not observed). This is a designed bound, not a revocation SLA. Token
revocation itself is bounded by access-token TTL and is a separate matter.

## D4. Direct-path migration model

1. Migration state `[CODE][DOC]`: downstreams stay reachable directly with
   their own OAuth (for example tg via `OAUTH_DCR_REDIRECT_URIS` including
   `https://mcp.mctl.ai/servers-callback`, `services/labs/mctl-telegram/values.yaml:151`
   at gitops `d1661f7`); `api.mctl.ai` accepts any same-issuer token. Gateway
   policy is presentation and narrowing only, not a security perimeter.
2. Audience-enforced API `[REF]`: T10. The gateway token sent directly is
   refused (401), the exchanged token is accepted, and the downstream never
   receives the caller bearer (asserted on every received `Authorization`
   header, which must verify as a downstream-audience token carrying `act`).
   Direct user tokens for `api.mctl.ai` stay valid, so only gateway-routed
   traffic is narrowed.
3. Enforced Tier A `[DOC]`: a downstream accepts only mctl-issued tokens for its
   audience and weaker direct doors are closed in G3.

Fixture success is architecture evidence. Production refusal at `api.mctl.ai`
is a post-G0 implementation acceptance criterion and is not claimed here.

## D5. Reference results

| Experiment | Tests | Outcome `[REF]` |
| --- | --- | --- |
| E1 identity/audience | T1-T5 | substitution refused both ways; exchange preserves `sub`/`pid`, adds `act`, bounded `exp`, no widening |
| E2 tenant/tool | T6-T9 | different catalogues; hidden = unknown; inert grants; cross-tenant refused twice |
| E3 bypass | T10 | gateway token refused directly, exchanged accepted, legacy state accepts (hazard shown) |
| E4 revocation | T11 | refused no later than R_max; newer version immediate |
| E5 failures | T12 | matrix below |
| G0.7 audit | T13 | one record per call with all fields; no credential or body text in any output |
| Roadmap | T14 | `python roadmap/scripts/validate.py roadmap/epics roadmap/spikes` |

Failure matrix (every row where authorization is unknown fails closed):

| Fixture | Outcome |
| --- | --- |
| policy unavailable, no cache | `tools/list` and `tools/call` fail with a retriable error; never empty, never allow-all (closed) |
| cache past hard TTL | same as unavailable (closed) |
| stale cache within hard TTL | last known policy applies; authorization is known, bounded by R_max (documented, not open) |
| exchange unavailable | the call fails, nothing reaches a downstream, no fallback credential (closed) |
| exchange returns invalid token | the call fails, token not forwarded (closed) |
| JWKS outage / removed `kid` | caller unauthorized (closed) |
| downstream unavailable on call | explicit retriable unavailable error, never success |
| downstream failure or malformed catalogue on list | server omitted and listed in `_meta.failed_servers`, `complete=false`; never presented as a complete catalogue |

Audit (T13): fields `principal, tenant, actor, server, tool, policy_version,
gateway_request_id, downstream_audit_id, decision, status`. The gateway id is
sent as `X-MCTL-Edge-Request-Id` with `X-MCTL-Edge-Route: gateway`; the
downstream audit id comes back in `X-MCTL-Audit-Id`. The record has no field
for a credential or body; the sink additionally counts redactions and the test
requires zero. Mapping to the production `audit_events` / `via_principal_id`
shape: unknown (not observed).

### E6. SDK comparison (D1 baseline kept)

| Dimension | mark3labs/mcp-go v1.0.0 | modelcontextprotocol/go-sdk v1.8.0 |
| --- | --- | --- |
| Stateless Streamable HTTP | `server.WithStateLess(true)`, already used by `mctl-api` `NewStreamableHTTPHandler` `[CODE]` | `StreamableHTTPOptions.Stateless`; GET/DELETE return 405 in that mode `[CODE: module]` |
| Proxy/aggregation | client package (`StreamableHTTP`, in-process) usable to fan out; no purpose-built aggregator observed | client present; no purpose-built aggregator observed; completeness of either for a pass-through proxy: unknown (not observed) |
| Filtering/interception | `WithToolFilter` (applied to list and call per `mctl-api` comment) and `WithToolHandlerMiddleware`, used in `mctl-api` `[CODE]` | `Server.AddReceivingMiddleware`, `RemoveTools`; ergonomics of rewriting `tools/list` results not exercised: unknown (not observed) |
| Protocol versions | `LATEST_PROTOCOL_VERSION` = 2026-07-28, `ValidProtocolVersions` list | `latestProtocolVersion` = 2026-07-28, five versions supported |
| Auth integration | per-request context hook `WithHTTPContextFunc`; no built-in server bearer middleware observed | `auth.RequireBearerToken(TokenVerifier)` with `TokenInfo` and `ResourceMetadataURL` in `WWW-Authenticate` |
| Maintenance/compat | reused library; `mctl-api` pins v1.0.0 (2026-09-02) while v1.3.0 (2026-10-14) exists: churn of three minors in six weeks | official org; v1.8.0 2026-09-04; heavier dependency set (`golang-jwt`, `oauth2`, `x/time`) |

Both require Go 1.25.x in `go.mod`. Conclusion: no evidence strong enough to
change D1; keep `mcp-go`. The gateway's boundary is independent of the
library (the model uses neither). Tasks, Apps and elicitation: pass-through
assumption only, no compatibility claimed.

Overhead `[REF]`, local in-process, one machine (Intel Xeon Skylake VM), Go
1.26, `-benchtime=2000x`: token verify about 0.12 ms, exchange about 0.33 ms,
policy lookup 1.5 us (hit) and 2.7 us (miss to an in-process source), full
`tools/call` with exchange and one loopback HTTP hop about 1.4 ms. These are
reference estimates, not production measurements. Component count added on the
call path: one hop (gateway) plus one exchange, which in production is a
network call to the mctl-api token endpoint: production latency unknown (not
observed).

Limitations: single process, fake clock, synchronous refresh, policy source
is a stub, the downstream is one reference service, `aud` is checked as a
string, no real HTTP JWKS fetch or caching, no MCP framing.

## GO criteria

| # | Criterion | Rating | Evidence |
| --- | --- | --- | --- |
| 1 | audiences prevent substitution in the reference contract | met | T1, T2, T10 `[REF]` |
| 2 | exchange preserves subject, mediation attributable | met | T3, T4 `[REF]` |
| 3 | credible code-grounded production path for direct-path enforcement | met-with-condition | hooks identified `[CODE]`; condition: aud enforcement ships before minting, and the Tier A verifiers in tg/coolify/seerrsense/projects/alice are unknown (not observed) |
| 4 | per-principal/tenant/tool narrowing without a second business-authz authority | met-with-condition | T6-T8 `[REF]`; condition: accepted D3 is `effective(P, S)` without a tenant key and the gateway needs a per-tool tenant-argument convention |
| 5 | fail-closed, operationally understandable | met-with-condition | T11, T12 `[REF]`; condition: plan wording "within one cache TTL" contradicts R_max |
| 6 | acceptable overhead for interactive MCP | met-with-condition | local reference numbers only; production hop latency unknown (not observed) |
| 7 | reuses capability, identity and audit contracts | met-with-condition | naming and principal reuse `[REF][CODE]`; `audit_events` mapping and `CapabilityGateway` reuse unknown (not observed) |

No criterion is `not met`. The evidence however contradicts accepted-design
text in four places, which by this spike's rule moves the verdict off `GO`:

1. The plan's G0 row bundles production mctl-api JWKS/audience work with a
   gateway prototype; #195 narrows G0 to a `.github` reference proof. The
   plan row should be split so the signing/JWKS/audience slice is its own
   mctl-api deliverable that precedes G1.
2. The plan's G1 acceptance "removing a grant hides the tool within one cache
   TTL" is not what the cache contract guarantees; it should state
   `R_max = hard_ttl + refresh_timeout` (and `soft_ttl` when the source is
   reachable).
3. D3 states `effective(P, S)` and a decision endpoint returning allowed tool
   names per principal; tenant-keyed calls need the tenant dimension
   (`effective(P, T, S)`) and the tenant-role membership in the policy
   document, plus a stated convention for how a call names its tenant.
4. The plan does not state the audience-before-mint ordering constraint.

## D6. Ownership map

Independently useful `mctl-api` slice (justified with or without the gateway):

| Gap | Repository | Issue | Dependency | Priority |
| --- | --- | --- | --- | --- |
| asymmetric signing, JWKS, `kid`/rotation, `aud` validation with drain rule (D2 hooks) | mctl-api | create issue | none; must precede any non-API audience mint | P1 |

Gateway-epic gaps (proposed only; not authorized):

| Gap | Repository | Issue | Dependency | Priority |
| --- | --- | --- | --- | --- |
| RFC 8707 `resource` on code/refresh grants; RFC 8693 grant restricted to gateway principal | mctl-api | create issue | slice above | P2 |
| tenant-keyed decision endpoint (`effective-tools`) distinct from native filtering | mctl-api | create issue, relate to mctl-api#488 | slice above | P2 |
| ceiling file in gitops, grants table | mctl-api, mctl-gitops | create issues | decision endpoint | P2 |
| Tier A mctl-token verification and principal mapping per downstream | each downstream repo (tg, seerrsense, projects; coolify unknown) | create per repo | slice above | P3 |
| gateway service | new repo `mctlhq/mctl-mcp-gateway` | create only after owner approval | revised plan | P3 |
| plan revisions 1-4 above (`docs/plans/mcp-gateway.md`) | mctlhq/.github | create issue (#194 is closed) | owner decision | P1 |
| confirm capability naming parity with `CapabilityGateway` | mctl-agents | note on completed mctl-agents#242 or new issue | none | P3 |

## D7. Proposed roadmap

The verdict is `REVISE`, so only the design revision and the focused
follow-ups above are proposed. The full epic and G1-G5 children (policy, Tier A,
downstream migration, cut-over, non-admin; the order accepted in #194) are not
drafted, and no issue, repository or roadmap epic was created.

## D8. Final decision

Verdict: REVISE
