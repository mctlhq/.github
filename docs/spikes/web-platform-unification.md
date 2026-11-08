# Web platform unification: cross-surface contracts

Research report for mctlhq/.github#328 (refs only, not closing). **PROPOSED / RESEARCH ONLY. NOT APPROVED.**
No owner decision is recorded here; `roadmap/spikes/web-platform-unification.yaml` stays
`lifecycle: proposed` with no decision. Scope follows the owner AMEND of 2026-11-08
(mctl-gitops proposal PR #2504): header behavior, asset integrity, six-surface inventory.
This report adds no code, benchmark, fixture, workflow, package, CSP or GitOps change.

## D0 Evidence discipline

Tags: `[CODE]` source at a pinned revision; `[CONFIG]` GitOps desired state, not the effective runtime;
`[LIVE]` read-only runtime observation; `[DOC]` issue, PR or prior report; `unknown (not observed)`
is never read as "absent", "off" or "passed".

Pins: `main` heads read through the GitHub API on 2026-11-08.

| Repository | Pin (40-hex) |
| --- | --- |
| mctlhq/mctl-web | `235e5361f6135287fbcaa9f63515e404e3e9c833` |
| mctlhq/mctl-telegram | `361c4fe237eed801decfe667943e79b3146a3e41` |
| mctlhq/mctl-docs | `543cca156f70a560bdeda5833378977c1af7628c` |
| mctlhq/mctl-academy | `e8fc1d76ccdd4b6ce2d4971c750f18005b5b4f6f` |
| mctlhq/mctl-alice | `eac08af19247c409379b0e82fb2bd5992f0b02a6` |
| mctlhq/seerrsense | `8224b9b511e729870251f81674a3098eae4d3ed8` |
| mctlhq/mctl-design | `7e1fbb0634d57a9d371e01f487785cd187b362c7` |
| mctlhq/mctl-gitops | `0db2e89e495424a300e072487e9c7d388160f7bd` |
| mctlhq/.github (baseline of this branch) | `b70a7536b9e7e93addf4e7bfd497c7a1fe57e770` |

### 0.20.2 release versus consumer state

| Item | Observation | Tag |
| --- | --- | --- |
| mctl-design#206 | MERGED at `7e1fbb0634d57a9d371e01f487785cd187b362c7` (equals design `main`) | `[DOC]` |
| mctl-web#239 | MERGED, merge commit `7902834b161579b3634f27753f133a328d0da0b5` | `[DOC]` |
| mctl-docs#202 | MERGED, `c3ddb0d956baf83384bdecb883e6810c1f67e307` | `[DOC]` |
| mctl-telegram#1245 | MERGED, `f35e4c1ddd1785c779bfe476eec58c310b13f2af` | `[DOC]` |
| mctl-academy#437 | MERGED, `78ed6939c5be405bd46b382f3f8a246d4bb47535` | `[DOC]` |
| mctl-alice#172 | MERGED, `eac08af19247c409379b0e82fb2bd5992f0b02a6` | `[DOC]` |
| seerrsense#117 | MERGED, `4753b5d1ed33090bcc4b18b369271b23d7773bc6` | `[DOC]` |
| Academy `@mctlhq/ui` range | `^0.20.2`, a caret, not an exact pin; tracked by academy#210 (OPEN) | `[CODE]` |
| GitOps desired image per consumer | `unknown (not observed)` in this run (values files not read) | |
| ArgoCD observed image per consumer | `unknown (not observed)` (no cluster read) | |
| Publish/deploy workflow results and the live mctl-design image | taken from the proposal `[DOC]`; not re-observed here | |

A merged PR is not evidence of a deployed consumer image, and neither is it evidence of effective
browser response headers. GitOps service directories exist for `admins`, `karabu`, `labs`, `namax66`
(names only, `[CONFIG]`); the consumer-to-tenant mapping beyond the issue text was not re-read.

## D1 Six-surface inventory

Cells marked `unknown` were not observed in this run.

| Surface | Runtime `[CODE]` | Design dependency | Public CSP (observed source) | Strict / auth routes |
| --- | --- | --- | --- | --- |
| mctl.ai | Nuxt `^4.3.1`, Vue `^3.5.44`, `@vueuse/core ^14.4.0` (mctl-web `package.json`); static files behind nginx | CDN `ui.mctl.ai` allowed in style-src, img-src | nginx: `default-src 'self'`; `script-src 'self' 'unsafe-inline'` plus stats, Cloudflare Insights and Turnstile origins; `style-src 'self' 'unsafe-inline'` plus fonts.googleapis.com and ui.mctl.ai; identical policy repeated in the server and four location blocks | none observed |
| tg.mctl.ai | Go net/http, html/template (`internal/web/headers.go`) | CDN versioned CSS on public chrome | `chromeCSP`: `default-src 'none'`; `script-src 'self' 'unsafe-inline' https://stats.mctl.ai`; `style-src 'self' 'unsafe-inline'` plus fonts.googleapis.com and ui.mctl.ai | OAuth/manage/connect lite pages: strict `default-src 'none'` with locally mirrored CSS per issue text `[DOC]`; the exact lite policy was not re-read: `unknown` |
| docs.mctl.ai | VitePress (version `unknown`, not re-read) | CDN | nginx: `script-src 'self' https://stats.mctl.ai __SCRIPT_SRC_HASHES__` (per-build hashes); `style-src 'self' 'unsafe-inline'` plus fonts and ui.mctl.ai | none observed |
| academy.mctl.ai | Vue `^3.5.13`, Vite `^6.4.3` (`client/package.json`), Hono API, Better Auth | `@mctlhq/ui ^0.20.2` plus CDN CSS | `server/middleware/security-headers.mjs`: `default-src 'self'`; `script-src 'self' https://stats.mctl.ai` (no inline); `style-src 'self' 'unsafe-inline' https://ui.mctl.ai https://fonts.googleapis.com` | per-route variation `unknown` |
| alice.mctl.ai | TypeScript native Node http, version 2.11.2 | locally vendored tokens/site.css via `sync:tokens` and `check:tokens` (alice `package.json`); no `@mctlhq/ui` dependency observed | per issue text `style-src 'self'`, `style-src-attr 'none'` `[DOC]`; headers source not re-read: `unknown` | OAuth/MCP route policies `unknown` |
| seerrsense.mctl.ai | Fastify `^5.12.3` | locally vendored via `check:tokens` script | public routes `unknown`; consent route `default-src 'none'`, `style-src 'self'` per issue text `[DOC]` | consent: as stated; others `unknown` |

Observations that follow from the source read:

- CSS permission never implies script permission. Telegram's public chrome allows inline script while its lite pages do not;
  Academy forbids inline script yet allows inline style.
- mctl.ai and Telegram chrome allow `'unsafe-inline'` scripts, which is a security property worth a separate review, not part of this spike.
- CI/security tests, packaging, rollback per surface: `unknown (not observed)`. Existing owners: gitops#500 (CI minimum).
- CDN versus vendored mode: mctl.ai, docs, Academy, Telegram public use the CDN; Alice and SeerrSense and Telegram lite use vendored CSS.

## D2 Header behavior contract

The per-surface behavior inventory (Escape, outside click, focus return, tab order, ARIA, resize, reduced
motion, no-JS) was **not observed** in this run: no browser run and no component source was read.
Every cell of that matrix is `unknown (not observed)` and is the first R0 follow-up.

Proposed framework-independent acceptance contract (testable, not implemented):

| Behavior | Requirement |
| --- | --- |
| Toggle control | native `button`; `aria-expanded` reflects state; `aria-controls` names the menu |
| Keyboard | Enter and Space toggle; Tab order follows DOM order; menu links are reachable only when open on narrow viewports |
| Escape | closes the open menu and returns focus to the toggle |
| Outside click | closes the menu without trapping focus |
| Focus | opening does not steal focus unexpectedly; closing restores it to the toggle when it was inside the menu |
| Resize | crossing the desktop breakpoint resets the state and leaves no stale `aria-expanded="true"` |
| Reduced motion | `prefers-reduced-motion: reduce` disables transitions |
| No-JS | navigation remains reachable (links visible or CSS-only disclosure) |

Adapter boundary (proposal only): keep Vue/VueUse in mctl.ai and Academy, VitePress theme in docs,
and Go/CSS-only in Telegram. A tiny vendor-pinned vanilla script is a **possible later pilot only on
script-permitted public Alice and SeerrSense routes**, and only after a separately authorized issue.
Telegram OAuth and lite pages and SeerrSense consent stay script-free; no CSP is relaxed.

## D3 CSS integrity and release contract (proposal)

Proposed immutable manifest per design release (illustrative, not shipped):

- exact `designVersion` (semver), source commit 40-hex, release provenance reference;
- per file: path, byte size, SHA-256, optional SRI SHA-384;
- CDN versioned path and the expected `Cache-Control: public, immutable` with a long max-age.

Preconditions for a cross-origin stylesheet with SRI: `integrity="sha384-..."`, `crossorigin="anonymous"`,
and a response with `Access-Control-Allow-Origin` matching the consumer. Without valid CORS the browser
refuses an SRI-checked cross-origin stylesheet. Whether `ui.mctl.ai` sends these headers is
`unknown (not observed)`.

| Route class | Allowed mode | Notes |
| --- | --- | --- |
| Public, style-src allows `ui.mctl.ai` | CDN with SRI, or vendored | failure falls back to unstyled but readable content; rollback by pinning previous exact version |
| Strict (`default-src 'none'`, consent, OAuth, lite) | vendored/embedded only | no new origin in CSP; local sync with `--check` compared to manifest hash |
| Redirect and error responses | unknown per surface | must be inventoried before any change |

Threat table: compromised or mutated CDN object (mitigated by SRI plus exact versions); mutable range
such as `^0.20.2` pulling unreviewed content (exact pin, academy#210); stale vendored copy
(manifest check in CI, design#34 and telegram#599); CDN outage (route-level fallback decision);
widened CSP for convenience (forbidden). Ownership is not duplicated: design#34, design#78,
telegram#599, academy#210, gitops#500 stay the owners.

## D4 Baseline and conditional triggers

- mctl.ai homepage transfer, JS bytes and load timing: `not-run` (no reproducible, non-disruptive method set up here).
- Actual Hono, Fastify and native-http constraints (OAuth, MCP, SSE, cookies, rate limit, error headers): only the dependency
  versions above were read; behavior inventory `unknown (not observed)`.
- Triggers for a later, separately authorized experiment (not a verdict): a Nuxt-versus-Astro comparison only after a measured
  loading, SEO or maintenance problem, and after comparing keep-Nuxt and optimize-Nuxt; a Hono-versus-Fastify/native comparison only
  after a concrete security, resource or maintenance issue and a plan to prove OAuth/MCP/SSE/cookie/error-header equivalence.
  No unmeasured benefit supports a migration.

## D5 Proposed ADR: Federated Web Platform

Status: **PROPOSED / RESEARCH ONLY.**

| Option | Summary | Cost / risk | Reversibility |
| --- | --- | --- | --- |
| A | keep runtimes; standardize header semantics, a11y tests, immutable assets (research hypothesis) | contract maintenance, several adapters | high |
| B | later pilot of a tiny adapter on script-permitted public routes | extra vendored artifact and its integrity check | high |
| C | defer until baselines and route observations exist | drift continues | n/a |
| D | targeted framework experiment only on a measured trigger | benchmark cost, security-equivalence proof | experiment is isolated |

Stakeholders: surface owners (six repos), mctl-design, GitOps, security review. Route CSP protections are
those in D1 and D3 and are not relaxed by any option.

Plan: R0 evidence and live release inventory; R1 header and integrity contracts; R2 separately authorized tiny pilot proposal;
R3 conditional benchmark if triggered; R4 human decision; R5 independently approved implementation. The portfolio site is a comparison
reference and is never edited.

## Owner decision gate

The owner selects one of `STANDARDIZE_CONTRACTS_KEEP_RUNTIMES`, `PILOT_SHARED_ADAPTERS`,
`TARGETED_MIGRATION_AFTER_BENCHMARK`, `DEFER_UNTIL_BASELINES`, `NO_ADOPTION`. The retain-runtime option
is a hypothesis, not a decision. Because large parts of D1 and D2 are `unknown`, `DEFER_UNTIL_BASELINES`
is a legitimate outcome until R0 completes.

## Gaps (honest list)

GitOps desired and ArgoCD observed images, effective headers, per-surface header behavior, CI tests, rollback,
and the performance baseline were not observed in this run.
