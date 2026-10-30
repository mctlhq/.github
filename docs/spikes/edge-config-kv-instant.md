# Edge config: Workers KV Instant vs Valkey for global runtime configuration

Spike report for mctlhq/.github#272 (carrier DevLoop; refs only, not closing).
Proposed verdict: `KEEP_VALKEY`. The owner has not recorded a decision, so the
manifest `roadmap/spikes/edge-config-kv-instant.yaml` stays `lifecycle: proposed`.

## Non-claims and evidence tags

This report changes nothing in production. It creates no Workers KV or KV
Instant namespace, Worker, route, token, Valkey resource or service. KV Instant
is never canonical: Git-reviewed configuration stays the source.

Every claim carries one tag. `[CODE]` is read at a pinned commit. `[DEPLOYED]`
is observed through mctl tools or gitops. `[DOC]` is issue or tool text.
`unknown (not observed)` means this spike did not see it; it is never read as
"absent" or zero. No secret, token, session or message body is quoted, only
paths and key patterns.

## Evidence pins

| Repository | Pin (40-hex) | Used for |
| --- | --- | --- |
| mctlhq/.github | `e5b88650776811067ca22da6d064653e249176a3` | roadmap schema, conventions |
| mctlhq/mctl-gitops | `691dfd3d060ef6219ef0881044e6adb60f983ef6` | Valkey manifests, Cloudflare IaC, service values |
| mctlhq/mctl-web | unknown (not observed) | `cloudflare-worker/` source was not available in this clone, so no pin |

The proposal text read mctl-gitops at `9462f41743a5be5620f3b843bc068c22ade610cb`;
that object is not in the worktree used here, so the pin above is the commit
actually read. The Valkey facts below were re-checked at the pin above.

## Valkey as deployed: an event bus, not a config store

- Single replica, image `valkey/valkey:8.1.10-alpine`, requests `25m` CPU /
  `64Mi`, limits `500m` / `300Mi`, plus a `redis_exporter` sidecar.
  `[CODE]` `platform-gitops/infra-components/data/valkey/statefulset.yaml`.
- AOF `appendfsync everysec`, `maxmemory 200mb`, `maxmemory-policy noeviction`.
  `[CODE]` `.../valkey/configmap.yaml`.
- Namespace `platform-events`. The data is Valkey Streams `mctl:events:*`
  (producer -> stream -> consuming session; `events/README.md`). `[CODE]`
- ACL (`.../valkey/externalsecret.yaml`), no secret values quoted: `default off`,
  producers may only `XADD`, `claude-remote` gets stream consumption plus plain
  keys on `mctl:events:state:*` only, `events-observer` reads the audit stream.
  No ACL user can read a configuration key namespace. `[CODE]`
- NetworkPolicy (`.../valkey/networkpolicy.yaml`) admits selected `labs` pods
  and `monitoring` only, so Valkey is unreachable from Cloudflare. `[CODE]`
- Clients are event producers or consumers only (`claude-remote`,
  `mctl-telegram-preview`, `mctl-api` `github-producer`). `[CODE]`

Conclusion: no runtime configuration is read from Valkey. `KEEP_VALKEY` is read
as "keep the current in-cluster configuration path (env vars, ConfigMaps, Git
and OpenTofu) and add no edge store". It does not say Valkey is the config store.

## Candidate inventory

| consumer | current source | where consumer runs | read rate | write/change rate | consistency need | payload size | sensitivity |
| --- | --- | --- | --- | --- | --- | --- | --- |
| model/provider/router policy | in-cluster workers; edge source unknown (not observed) | in cluster `[DEPLOYED]`; edge unknown (not observed) | unknown (not observed) | unknown (not observed) | unknown (not observed) | unknown (not observed) | policy; no secrets in projection |
| kill switches and feature flags (`AGENT_KILL_SWITCH`, `AGENT_ENABLED`, `HUMAN_INPUT_ENABLED`, `otel.enabled`) | GitOps env vars and values, `services/labs/mctl-telegram-preview/values.yaml`, `services/admins/mctl-agents-worker-exec/values.yaml` `[CODE]` | in-cluster pods | read at process start; rate unknown (not observed) | change via ArgoCD sync and rollout; flips per month unknown (not observed) | rollout latency acceptable (current design) | a few booleans | low |
| `mctl-events-policy` | ConfigMap mounted at `/etc/mctl-events/policy.json`, `services/labs/claude-remote/values.yaml` `[CODE]` | in-cluster pod | file read; rate unknown (not observed) | Git change; rate unknown (not observed) | eventual | unknown (not observed) | low |
| tenant/domain to backend routing | ingress hosts in service `values.yaml`; Cloudflare zone config under `infrastructure/cloudflare/zones/` applied by OpenTofu `[CODE]` | Kubernetes ingress and Cloudflare zone rules (declarative, no runtime key read) | not a key-value read | Git change | eventual | n/a | low |
| capability and gateway eligibility | mctl-api in cluster; Cloudflare MCP portal allowlists (`infrastructure/cloudflare/portal/`, applied by OpenTofu) `[CODE]` | in-cluster mctl-api; Cloudflare consumes the portal config itself | unknown (not observed) | Git change | unknown (not observed) | unknown (not observed) | authority state; never moved to Cloudflare |
| `mctl-landing-form` Worker | code constants and session/request KV `[CODE]` for the IaC side only (`infrastructure/cloudflare/account/workers-kv.tf`, `zones/mctl-me/workers.tf`); script source unknown (not observed) | Cloudflare edge `[CODE]` | unknown (not observed) | unknown (not observed) | unknown (not observed) | unknown (not observed) | session and access-request data: excluded by invariant 2 |

The Worker is the only edge code MCTL runs. Its KV namespaces
(`mctl-web-sessions[-dev]`, `mctl-web-access-requests[-dev]`) hold session and
request state, which is not configuration. Whether its bot filter or redirect
logic reads a runtime-updatable rule set is unknown (not observed), because the
source in `mctlhq/mctl-web` `cloudflare-worker/` was not read. This is the one
open input to the gate (see re-open triggers).

## Gate result

On the evidence read, every shared-configuration consumer runs inside the MCTL
cluster and is served by env vars, ConfigMaps or declarative IaC. No observed
edge consumer reads shared runtime configuration on a hot path, so the edge
round-trip argument does not apply and the KV Instant experiment is not run.

## KV Instant snapshot

Availability: private beta, not stable. Limits mentioned in the issue (1 MB and
10k keys per namespace, expensive writes) are `[DOC]` from the issue and were
not re-read from Cloudflare documentation or pricing in this run:
unknown (not observed) for current limits and pricing, with no read date or
source URL to cite. Whether the MCTL Cloudflare account has beta access:
unknown (not observed). No latency figure for KV Instant is published here.

## Comparison and cost note

| Dimension | Valkey (as deployed) | KV Instant |
| --- | --- | --- |
| Config read path today | none (Streams bus) `[CODE]` | none |
| Consumer locality | in-cluster only (NetworkPolicy) `[CODE]` | global edge, private beta |
| Read latency | unknown (not observed) | unknown (not observed) |
| Write cost / propagation | unknown (not observed) | unknown (not observed) |
| Operational owner | `platform-events` infra-components `[CODE]` | unknown (not observed) |
| Cost | already paid: requests `25m` / `64Mi`, limits `500m` / `300Mi`, single replica; no monetary figure observed | formula only: snapshot bytes x stored copies + writes/month x write price + reads/month x read price; no inputs measured |

No monetary figure is claimed, and no adoption recommendation follows from the
formula. Other issue comparison rows (failover, size limits, audit, etc.) were
not measured: unknown (not observed).

## Re-open triggers and preconditions

Re-open to `HYBRID_EDGE_CONFIG` (or `DEFER_PRIVATE_BETA` if beta access is
unavailable, with no invented latency) if any of these appears: the Worker
source review finds a runtime-updatable rule set; a Worker or edge MCP entry
point must reject or route by tenant or kill switch before reaching the origin;
KV Instant reaches GA. Any follow-up inherits these invariants as preconditions:

1. Projection only: Git-reviewed config is the source; the edge copy is derived.
2. No session, approval, audit, receipt, secret, token or message body content.
3. Safe, fail-closed fallback when the projection is missing or stale.
4. Narrow-only: the projection may restrict, never widen, access.
5. Atomic, immutable, versioned snapshots with a `current` pointer.
6. Valkey and the in-cluster path are kept.
7. No production cutover as part of the spike.

If an edge consumer is proven and beta access exists, a hybrid verdict needs
measured p50/p95/p99, cold read, propagation, origin-down and rollback
behaviour, and a cost model from measured bytes and rates first.

## Verdict

`KEEP_VALKEY`: the first gate finds no observed edge consumer of shared
runtime configuration; the in-cluster path is kept and nothing is added at the
edge. Caveat: the Worker source was not read, so the gate is conditional on the
re-open trigger above. Optional in-cluster Valkey `GET` baseline was not run
(no config key exists and the ACL permits no config reader).
