# MCP Gateway

Status: proposal, 2026-10-17. Decisions D1-D5 were accepted by the owner on
2026-10-17 as recommended (see "Decisions"). Nothing here is implemented yet;
implementation starts from the epic and issues materialized after this
document merges.

## Goal

Replace the user-blind Cloudflare MCP Portal at `mcp.mctl.ai` with an
mctl-owned gateway that decides, per principal and per tenant, **which
downstream MCP servers and which individual tools** a caller can see and call.
The gateway is one endpoint for every MCP client. Downstream servers keep their
own authorization; the gateway can only narrow what they would allow, never
widen it.

Non-goals:

- No new authorization authority. mctl-api stays the source of identity,
  tenant membership and roles; the gateway asks, it does not decide alone.
- No vault of per-user upstream OAuth grants (see "Downstream credentials").
- No DLP or content inspection in the first release.
- No change to Local Bridge session custody: MTProto sessions stay on the
  user's device (mctl-telegram#615).

## Why the portal cannot do this

Measured, not assumed:

- The portal's tool allowlist is one switch per server and the same for every
  user. mctl-telegram#629 says so in its allowlist ("The Cloudflare portal
  allowlist is a per-server switch and user-blind"), and mctl-api#297 exposes
  all of its tools because "visibility, not permission" is all the portal
  offers.
- Cloudflare Gateway sees portal traffic at DNS only. No L7 rule ever fired
  and no Gateway dataset carries `Mcp-Method` or `Mcp-Name`
  (mctl-gitops#1181). Tool-level policy cannot be delegated to Cloudflare.
- The portal is "an aggregate view, not a chokepoint" (mctl-telegram#616):
  every downstream is its own protected resource with its own authorization
  server, so a caller can always go around the portal.
- The portal's configuration has six writers (mctl-gitops#1370), and parts of
  its Access setup were made in the dashboard (mctl-gitops#1296, #1328).

The only per-user tool filter that exists today is inside mctl-api:
`ownerOnlyToolFilter` (`internal/mcp/owner_tools.go`) hides three tools from
callers who are not owners of one tenant, and re-checks on `tools/call`. The
gateway generalizes that pattern across servers.

## Current state (audited 2026-10-17)

Pinned revisions: mctlhq/.github `33af5ff1`, mctl-gitops `29b3372b`,
mctl-api `7b67d82a`, mctl-agents `63c882fc`.

| Server | Upstream | How it authenticates a user today | Per-user? | ZITADEL app |
| --- | --- | --- | --- | --- |
| api | `api.mctl.ai/mcp` | own OAuth 2.1 server (PKCE, DCR), ZITADEL as sign-in upstream; also accepts ZITADEL JWTs via `MCTL_OIDC_PROVIDERS` | yes, tenants and roles | yes |
| tg | `tg.mctl.ai/mcp` | own OAuth server, Telegram OIDC sign-in | yes, own Telegram account | no |
| coolify | `coolify.mctl.ai/mcp` | own OAuth server (CIMD, DCR fallback); GitHub, Google or ZITADEL sign-in | yes, `MCP_TENANCY: multi` | yes |
| projects | `projects.mctl.ai/mcp` | Cloudflare Access managed OAuth; pod checks the Access assertion; `grants.yaml` per email | yes, per email | no |
| seerrsense | `seerrsense.mctl.ai/mcp` | own OAuth server, Google sign-in | yes, per email | no |
| alice | `alice.mctl.ai/mcp` | own OAuth server, open DCR; one owner credential behind it | no, single owner | no |

The portal exposes 156 tools across these six servers. Only platform admins
can sign in to it: the ZITADEL project "Cloudflare Access" grants its role to
admins only. Its Access session lasts one year.

Relevant facts for the design:

- mctl-api signs its own access tokens with a shared HMAC secret
  (`internal/auth/oauth_server.go`), so nothing else can verify them offline.
- No RFC 8693 token exchange exists anywhere: not in mctl-api, not in the
  ZITADEL configuration (`zitadel-iac` grants only authorization code and
  refresh token).
- In ZITADEL every application in a project receives the project's client IDs
  in `aud`, so each consumer that needs its own audience needs its own project
  (`zitadel-iac/iac/mctl-api.tf`).
- mctl-api's `/mcp` is stateless Streamable HTTP without `Mcp-Session-Id`, and
  each tool handler calls mctl-api REST with the caller's bearer, so REST
  authorization is re-applied on every call.
- mctl-api#377 (principal-based tenant authorization) is in shadow mode until
  at least 2026-10-19. Per-principal tool policy depends on it being enforced.
- Cluster: three cx43 workers. CPU is 11-16% used; memory is 60-73% used and
  limits are over-committed (112-125%). A gateway with 128-256 Mi requests per
  replica fits; no new node is needed or allowed.

Constraints inherited from the paused `enterprise-mcp` epic (.github#35):

1. The edge may deny more, never grant more than mctl permits.
2. Fail-closed tool exposure: a tool nobody listed is hidden (.github#44,
   mctl-gitops#1922).
3. Keep the correlation contract from mctl-telegram#617: forward
   `Mcp-Method`/`Mcp-Name` faithfully, carry an edge route and request id into
   downstream audit.
4. Modern stateless Streamable HTTP; no dependence on SSE or `Mcp-Session-Id`.
5. Tasks and Apps pass through untouched once measured (.github#41,
   mctl-api#284, mctl-telegram#650).

## Architecture

```
MCP client ──► Cloudflare proxy (WAF, DDoS, TLS) ──► mcp-gateway (k3s)
                                                        │  1. authenticate caller (token for aud=mcp.mctl.ai)
                                                        │  2. fetch effective policy for principal (mctl-api, cached)
                                                        │  3. tools/list: merge downstream lists, drop what policy hides
                                                        │  4. tools/call: re-check policy, then forward
                                                        ▼
                     downstream MCP servers (api, tg, coolify, projects, seerrsense, alice)
                     each validates a token minted for ITS audience and re-checks its own authz
```

Three parts, each with one owner:

| Part | Owner | Holds |
| --- | --- | --- |
| Data plane: `mcp-gateway` service | new repo `mctlhq/mctl-mcp-gateway` | no durable state; a short-TTL policy cache and the merged tool catalogue |
| Policy: who may use which tool | mctl-api (Postgres) | grants per principal, tenant role or group; decision endpoint; audit |
| Ceiling: the most any principal can ever get | mctl-gitops (reviewed, public) | per-server list of tool names that may be granted at all |

### Why a separate service, not a module inside mctl-api

- Blast radius: the gateway parses untrusted MCP traffic for six servers. A
  bug or memory blow-up there must not take down the platform API.
- Independent scaling and rollout: the gateway can be redeployed, rolled back
  or rate-limited without an mctl-api release.
- mctl-api is itself one of the downstreams. Putting the gateway inside it
  would make one process both the judge and a party.
- The cost is one more hop for policy lookups, which the cache absorbs.

The alternative is listed as decision D1.

### Identity at the gateway

The caller presents an access token whose audience is the gateway
(`https://mcp.mctl.ai`). Two ways to issue it:

- **mctl-api's authorization server** (recommended). It already does PKCE and
  DCR for MCP clients, signs users in through ZITADEL and maps them to
  principals. Needed changes: asymmetric signing with a published JWKS, and an
  audience per resource, so the gateway (and later the downstreams) verify
  tokens offline.
- **ZITADEL directly.** ZITADEL is already the IdP, but MCP clients expect
  DCR or CIMD, and the ZITADEL setup here has neither. It would mean
  pre-registering every MCP client by hand.

Either way the token carries a stable principal id (`prn_...`), never a login.

### Policy model

A grant says: subject × server × tool → allow. Subjects are a principal, a
tenant role (`owner`, `developer`, `viewer` of tenant T) or a platform group
(`admin`). Tool can be an exact name or the whole server. Nothing else, in
particular no deny rules: the effective set is the union of grants, capped by
the ceiling, which keeps every decision explainable.

```
effective(P, S) = ceiling(S) ∩ ( ⋃ grants matching P, S )
```

- Grants live in mctl-api, not in gitops: mctl-gitops is public and a grant
  names principals and tenants.
- The ceiling lives in gitops and is the reviewed, user-independent part. It
  replaces today's `infrastructure/cloudflare/portal/allowlists/` and keeps
  its fail-closed rule: a new downstream tool is not grantable until it is
  listed.
- mctl-api's existing `ownerOnlyToolFilter` becomes the first set of grants
  expressed in this model, and the code filter stays as defense in depth.
- Decision endpoint (mctl-api):
  `GET /api/v1/mcp/effective-tools?principal=prn_...` returns, per server, the
  allowed tool names plus a policy version. The gateway authenticates as a
  service principal; the endpoint is not callable by end users for others.

### Request handling

- `tools/list`: fan out to the servers the principal has any grant on, merge,
  drop every tool not in `effective`, prefix names with the server id
  (`tg_send_message`), as the portal does today so clients keep working.
- `tools/call`: resolve the prefix, check `effective` again (a list result is
  never trusted as permission), then forward. A denied call returns the same
  error as an unknown tool, so hidden tools are not enumerable.
- Prompts and resources follow the same rule as tools.
- Pass-through, not interpretation: the request and response bodies are
  forwarded unchanged apart from the name prefix. Tasks, Apps and elicitation
  pass through; nothing is cached across calls.

### Failure semantics

Following the workspace rule that "could not observe" is never "observed
absent":

| Situation | Result |
| --- | --- |
| Policy endpoint unreachable, no cached policy | `tools/list` and `tools/call` fail with a retriable error; never an empty list, never "allow all" |
| Cached policy older than its hard TTL | same as unreachable |
| One downstream fails during `tools/list` | the response omits it **and** says so (`_meta` with the failed server ids); the gateway's own health turns not-ready |
| Ceiling file missing or unparsable at start | the gateway does not start |
| Downstream token mint fails | that call fails; no fallback credential |

### Downstream credentials

The gateway must call each downstream as the user, not as itself, so the
downstream's own checks still run per user. Storing every user's upstream
OAuth grants in the gateway would make it the most valuable target in the
cluster, so the design avoids it. Each downstream instead goes into one of
three tiers:

| Tier | How the gateway calls it | Servers |
| --- | --- | --- |
| A — trusts mctl identity | gateway exchanges the caller's token for one with the downstream's audience (RFC 8693 at the mctl-api token endpoint); downstream verifies it via JWKS and maps the principal to its own user | api now; coolify (already accepts ZITADEL); tg, seerrsense, projects after they add a principal link |
| B — admin-only, single owner | gateway uses one service credential held in Vault; only platform admins get grants | alice |
| C — not integrated | stays reachable directly, not through the gateway | anything that cannot do A or B |

Tier A is also what closes the bypass of mctl-telegram#616: once a downstream
accepts only mctl-issued tokens, there is no second door with weaker rules.
For tg this needs a link from a principal to a Telegram identity, which
mctl-api's surface identity links (`surface_identity_links`) already model.

### Audit and correlation

- The gateway writes one record per `tools/call`: principal, server, tool,
  decision, policy version, edge request id, latency, downstream status. It
  goes to mctl-api's `audit_events` (with `via_principal_id` = the gateway's
  service principal) rather than a new store.
- The gateway sets the same headers the portal sets today
  (`Mcp-Method`, `Mcp-Name`) plus `X-MCTL-Edge-Route: gateway`, so the
  downstream audit added in mctl-telegram#617 keeps working.

### Deployment

- Go, on mcp-go, the library mctl-api already uses; its tool filter hook is
  the same one `ownerOnlyToolFilter` uses.
- Two replicas, 128 Mi request each, `base-service` chart, behind Cloudflare
  proxy like every other public host.
- New host `gw.mcp.mctl.ai` during migration; `mcp.mctl.ai` moves only at the
  end.

## Migration

Each phase ends with a check that can fail. The portal keeps running until the
last phase.

| Phase | Delivers | Done when |
| --- | --- | --- |
| G0 — spike | mctl-api JWKS signing and per-resource audience behind a flag; gateway prototype in front of `api` only | a token for `aud=mcp.mctl.ai` reaches mctl-api tools through the gateway; a token for another audience is refused |
| G1 — policy | grants table, ceiling in gitops, decision endpoint, `ownerOnlyToolFilter` expressed as grants | two test principals see different tool lists from the same server; removing a grant hides the tool within one cache TTL; mutation-verified |
| G2 — tier A for api and coolify | token exchange at mctl-api, coolify accepts mctl audience | calls reach both as the real user; downstream audit shows the edge route |
| G3 — tg, seerrsense, projects | principal links and mctl-token acceptance in each | each server reached through the gateway as the user; its direct OAuth door narrowed or closed |
| G4 — alice as tier B; cut-over | `mcp.mctl.ai` points at the gateway | all current portal users work; portal Access apps and allowlists deleted in a follow-up PR |
| G5 — non-admin users | tenant members get grants | a tenant owner sees only their tenant's tools; the denied-user check of mctl-telegram#616 passes |

G1 needs mctl-api#377 enforced (principal mode); before that, policy can only
be tested, not relied on.

## Relationship to the enterprise-mcp epic

`enterprise-mcp` (.github#35) is paused. Its gateway phase concluded that
Cloudflare cannot enforce tool policy (mctl-gitops#1181), and its open
endpoint decision (.github#128) is exactly the question this proposal answers
for the platform: an mctl-owned gateway, with Cloudflare as the outer proxy
only. Decision D4 settles how the two relate.

## Risks

- **The gateway becomes a single point of failure for every MCP client.**
  Mitigated by two replicas, the portal running in parallel until G4, and
  downstreams staying reachable directly until G3 closes each door.
- **Policy drift between the ceiling and grants.** A grant for a tool outside
  the ceiling is inert; a nightly check reports such grants.
- **Token exchange widens mctl-api's role.** It already is the authorization
  server for MCP; the change adds audiences, not authority. Exchange is
  restricted to the gateway's service principal.
- **MCP protocol churn.** The gateway forwards bodies unchanged and keeps
  stateless transport, so new methods pass through; only the list/call paths
  are interpreted.

## Decisions

Accepted by the owner on 2026-10-17, each as recommended.

- **D1 — Separate service or inside mctl-api.** Decision: separate
  service, for blast radius and because mctl-api is itself a downstream.
- **D2 — Who issues the caller's token.** Decision: mctl-api's
  authorization server, made verifiable via JWKS; ZITADEL stays the IdP behind
  it. ZITADEL directly would need hand-registered clients.
- **D3 — Where grants live.** Decision: mctl-api Postgres, with the
  ceiling in gitops. Gitops-only grants would publish principals and tenants
  in a public repo.
- **D4 — Epic.** Decision: a new `mcp-gateway` epic that supersedes the
  gateway phase of `enterprise-mcp` and answers .github#128, rather than
  resuming the paused epic as it stands.
- **D5 — Who gets access first.** Decision: platform admins only until
  G5, then tenant owners, then tenant members.
