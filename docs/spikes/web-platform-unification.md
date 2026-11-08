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
| mctlhq/mctl-telegram | `3107baf5a4b0608fb33e905b7529dbd45a7af30f` |
| mctlhq/mctl-docs | `543cca156f70a560bdeda5833378977c1af7628c` |
| mctlhq/mctl-academy | `e8fc1d76ccdd4b6ce2d4971c750f18005b5b4f6f` |
| mctlhq/mctl-alice | `eac08af19247c409379b0e82fb2bd5992f0b02a6` |
| mctlhq/seerrsense | `8224b9b511e729870251f81674a3098eae4d3ed8` |
| mctlhq/mctl-design | `7e1fbb0634d57a9d371e01f487785cd187b362c7` |
| mctlhq/mctl-gitops | `4e24447c1cbcc0012fe5e1ae8ff65ff034b0c4cc` |
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
| GitOps desired image per consumer | Re-observed in R0 AMEND below; image pins match ArgoCD's reported image tags | `[CONFIG]` + `[LIVE]` |
| ArgoCD observed image per consumer | Read-only service status retrieved for all six; `Healthy / Synced` for each, details below | `[LIVE]` |
| Publish/deploy workflow results and the live mctl-design image | taken from the proposal `[DOC]`; not re-observed here | |

A merged PR alone is not deployment evidence. The **R0 AMEND** below independently re-reads GitOps desired state and ArgoCD observed image/status, and confirms six consumers are on updated service image tags. This does **not** establish their effective browser response headers, CDN HTTP integrity/CORS headers, or route-level functional behavior.

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
- Cross-site CI/security tests, browser-level header behavior and rollback mechanics remain only partially inventoried, not executed. Academy's `AppNav.test.ts` was identified; CI policy remains owned by gitops#500.
- CDN versus vendored mode: mctl.ai, docs, Academy, Telegram public use the CDN; Alice and SeerrSense and Telegram lite use vendored CSS.

## D2 Header behavior contract

The initial investigator did not read header implementations. The **R0 AMEND source review** below now confirms toggle, Escape, outside click, focus behavior and responsive breakpoints in four implementations; Telegram uses a CSS checkbox and Docs delegates to VitePress. These are **[CODE] observations only**, not browser/a11y test results. Reduced-motion, no-JS rendering and focus trap/tab order remain **unknown (not browser-tested)**.

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

**After R0 AMEND:** desired GitOps image tags and ArgoCD observed image tags are now verified, and current header source implementations are inventoried below. Remaining unknowns: **effective HTTP response headers**, full route matrix for redirects/errors, browser-tested keyboard/focus/a11y and no-JS behavior, CDN cache/CORS/SRI response headers, complete CI/rollback parity, and reproducible performance measurements.


## R0 AMEND — independently verified evidence (2026-11-08)

This addendum closes the specific R0 gaps identified on review of PR #329. Evidence is scoped to the **source/configuration and read-only MCTL service APIs**, not to an HTTP/browser test. It does not create an architectural verdict. Source pins are in D0 (Telegram and GitOps pins were refreshed above); all links below use an immutable pinned ref where available.

### R0.1 Six-site design adoption: source → GitOps → ArgoCD

[CODE] The six consumers now explicitly reference mctl-design `0.20.2`:
[mctl-web Nuxt config](https://github.com/mctlhq/mctl-web/blob/235e5361f6135287fbcaa9f63515e404e3e9c833/nuxt.config.ts),
[docs VitePress config](https://github.com/mctlhq/mctl-docs/blob/543cca156f70a560bdeda5833378977c1af7628c/docs/.vitepress/config.ts),
[Telegram DesignVersion](https://github.com/mctlhq/mctl-telegram/blob/3107baf5a4b0608fb33e905b7529dbd45a7af30f/internal/ui/chrome.go),
[Academy HTML](https://github.com/mctlhq/mctl-academy/blob/e8fc1d76ccdd4b6ce2d4971c750f18005b5b4f6f/client/index.html),
[Alice local CSS sync](https://github.com/mctlhq/mctl-alice/blob/eac08af19247c409379b0e82fb2bd5992f0b02a6/scripts/sync-tokens.mjs),
[SeerrSense local CSS sync](https://github.com/mctlhq/seerrsense/blob/8224b9b511e729870251f81674a3098eae4d3ed8/scripts/sync-tokens.mjs).
The Academy Vue dependency remains **`@mctlhq/ui: ^0.20.2`**, a range rather than an exact version; [academy#210](https://github.com/mctlhq/mctl-academy/issues/210) remains the separate ownership item.

| Surface / service | GitOps desired image [CONFIG] | ArgoCD observed image [LIVE] | ArgoCD status [LIVE] |
| --- | --- | --- | --- |
| [mctl.ai / mctl-web](https://github.com/mctlhq/mctl-gitops/blob/4e24447c1cbcc0012fe5e1ae8ff65ff034b0c4cc/platform-gitops/services/admins/mctl-web/values.yaml) | `7.21.1` | `7.21.1` | Healthy / Synced |
| [tg.mctl.ai / mctl-telegram](https://github.com/mctlhq/mctl-gitops/blob/4e24447c1cbcc0012fe5e1ae8ff65ff034b0c4cc/platform-gitops/services/labs/mctl-telegram/values.yaml) | `0.137.1` | `0.137.1` | Healthy / Synced |
| [docs.mctl.ai / mctl-docs](https://github.com/mctlhq/mctl-gitops/blob/4e24447c1cbcc0012fe5e1ae8ff65ff034b0c4cc/platform-gitops/services/admins/mctl-docs/values.yaml) | `0.1.66` | `0.1.66` | Healthy / Synced |
| [academy.mctl.ai / mctl-academy](https://github.com/mctlhq/mctl-gitops/blob/4e24447c1cbcc0012fe5e1ae8ff65ff034b0c4cc/platform-gitops/services/labs/mctl-academy/values.yaml) | `0.1.76` | `0.1.76` | Healthy / Synced |
| [alice.mctl.ai / mctl-alice](https://github.com/mctlhq/mctl-gitops/blob/4e24447c1cbcc0012fe5e1ae8ff65ff034b0c4cc/platform-gitops/services/labs/mctl-alice/values.yaml) | `2.13.1` | `2.13.1` | Healthy / Synced |
| [seerrsense.mctl.ai / seerrsense](https://github.com/mctlhq/mctl-gitops/blob/4e24447c1cbcc0012fe5e1ae8ff65ff034b0c4cc/platform-gitops/services/labs/seerrsense/values.yaml) | `1.14.1` | `1.14.1` | Healthy / Synced |
| [ui.mctl.ai / mctl-design](https://github.com/mctlhq/mctl-gitops/blob/4e24447c1cbcc0012fe5e1ae8ff65ff034b0c4cc/platform-gitops/services/admins/mctl-design/values.yaml) | `7e1fbb0634d5` | `7e1fbb0634d5` | Healthy / Synced |

[LIVE] Values are from authenticated read-only `mctl_get_service_status` responses on 2026-11-08; they indicate **ArgoCD resource state**, not live-page content delivery or browser-loaded CSS. `mctl-design` Publish and Deploy CI succeeded previously; no CDN HTTP HEAD/GET/CORS/cache verification was made during this amendment. Telegram uses GitOps `strategy.type: Recreate` for MTProto session custody; do not modify that strategy as part of web-platform unification. Alice has its own rollout-strategy notes in GitOps and should be independently assessed before any future deployment change.

### R0.2 Header implementation evidence (source, not browser tests)

| Surface | Source behavior confirmed [CODE] | Still unverified |
| --- | --- | --- |
| [mctl.ai](https://github.com/mctlhq/mctl-web/blob/235e5361f6135287fbcaa9f63515e404e3e9c833/app/components/AppHeader.vue) | VueUse `onClickOutside`, `onKeyStroke('Escape')`, route-change close; returns focus to burger on Escape, closes at min-width 981px; native button + `aria-expanded` | browser Tab sequence, reduced motion, no-JS |
| [Academy](https://github.com/mctlhq/mctl-academy/blob/e8fc1d76ccdd4b6ce2d4971c750f18005b5b4f6f/client/src/components/AppNav.vue) | Vue: document `keydown` Escape, `pointerdown` outside, navigation close, matchMedia min-width 981px close; burger `aria-expanded` and focus return; [component tests exist](https://github.com/mctlhq/mctl-academy/blob/e8fc1d76ccdd4b6ce2d4971c750f18005b5b4f6f/client/src/components/__tests__/AppNav.test.ts) | full browser a11y and responsive visual tests |
| [Alice](https://github.com/mctlhq/mctl-alice/blob/eac08af19247c409379b0e82fb2bd5992f0b02a6/public/assets/site.js) | Locally served vanilla JS closes on Escape, outside pointer, link click and resize above 980px; `aria-expanded` and focus return; native burger button in [HTML](https://github.com/mctlhq/mctl-alice/blob/eac08af19247c409379b0e82fb2bd5992f0b02a6/public/index.html) | no-JS navigation and browser focus tests |
| [SeerrSense](https://github.com/mctlhq/seerrsense/blob/8224b9b511e729870251f81674a3098eae4d3ed8/public/assets/site.js) | Closely parallel vanilla JS handlers, but breakpoint is **768px** rather than 981px; public [HTML](https://github.com/mctlhq/seerrsense/blob/8224b9b511e729870251f81674a3098eae4d3ed8/public/index.html) has native burger button | no-JS navigation, breakpoint intention and browser focus tests |
| [Telegram](https://github.com/mctlhq/mctl-telegram/blob/3107baf5a4b0608fb33e905b7529dbd45a7af30f/internal/ui/chrome.go) | Public header uses CSS-only checkbox + label, not a shared JS drawer; strict lite header intentionally has no drawer | checkbox keyboard/label semantics need browser a11y testing; some auth flows have nonce scripts unrelated to header |
| [Docs](https://github.com/mctlhq/mctl-docs/blob/543cca156f70a560bdeda5833378977c1af7628c/docs/.vitepress/theme/index.ts) | VitePress DefaultTheme navigation wrapped by custom Layout; theme CSS bridges design tokens | actual VitePress nav behavior was not inspected; browser keyboard/no-JS tests `not-run` |

The strongest candidate for a **later, separately approved** narrow adapter is the near-identical Alice/SeerrSense public drawer logic, not a cross-framework rewrite. The distinct 981px vs 768px thresholds are product constraints to evaluate rather than silently normalize. No `site-header.js` is implemented in this PR.

### R0.3 Source-derived CSP boundaries and remaining security unknowns

| Site / path class | Source policy finding [CODE] | Asset implications |
| --- | --- | --- |
| mctl.ai nginx public | [nginx](https://github.com/mctlhq/mctl-web/blob/235e5361f6135287fbcaa9f63515e404e3e9c833/nginx.conf) explicitly admits `ui.mctl.ai` on `style-src` and inline scripts | CDN styles allowed in source; effective edge HTTP headers unobserved |
| Docs VitePress public | [nginx](https://github.com/mctlhq/mctl-docs/blob/543cca156f70a560bdeda5833378977c1af7628c/nginx.conf) hashes inline scripts and permits `ui.mctl.ai` styles | CDN styles allowed in source; effective headers unknown |
| Academy public/API errors | [Hono security-header middleware](https://github.com/mctlhq/mctl-academy/blob/e8fc1d76ccdd4b6ce2d4971c750f18005b5b4f6f/server/middleware/security-headers.mjs) defines CDN-allowed `style-src`, self/analytics `script-src`, and separate error-response helpers | Inline style needed by Vue; error handling has code for headers, not live-tested |
| Alice site/OAuth | [Node contentSecurityPolicy](https://github.com/mctlhq/mctl-alice/blob/eac08af19247c409379b0e82fb2bd5992f0b02a6/src/server/http-server.ts) sets `style-src 'self'`, `style-src-attr 'none'`, `script-src 'self' https://stats.mctl.ai`, `script-src-attr 'none'`; OAuth redirect can add a validated `form-action` source | local CSS/JS, no new `ui.mctl.ai` stylesheet permission |
| Telegram public chrome | [chromeCSP](https://github.com/mctlhq/mctl-telegram/blob/3107baf5a4b0608fb33e905b7529dbd45a7af30f/internal/web/headers.go) admits CDN CSS and inline page scripts | not representative of connect/manage/OAuth routes |
| Telegram connect/manage | [connect.go](https://github.com/mctlhq/mctl-telegram/blob/3107baf5a4b0608fb33e905b7529dbd45a7af30f/internal/web/connect.go) defaults to `default-src 'none'` + inline styles and local forms; [manage](https://github.com/mctlhq/mctl-telegram/blob/3107baf5a4b0608fb33e905b7529dbd45a7af30f/internal/web/manage.go) also has strict policy | no external JS/CSS for these flows; **connect success and some OAuth pages have narrowly authorized inline scripts**, so do not claim all strict routes are script-free |
| Telegram enable_access | [enable-access page](https://github.com/mctlhq/mctl-telegram/blob/3107baf5a4b0608fb33e905b7529dbd45a7af30f/internal/oauth/enable_access_page.go) comments document nonce-authorized inline scripts for password show/hide and success redirect | nonce does **not** imply permission to introduce general header JS |
| SeerrSense OAuth consent | [route](https://github.com/mctlhq/seerrsense/blob/8224b9b511e729870251f81674a3098eae4d3ed8/src/auth/routes.ts) sets `default-src 'none'; style-src 'self'; img-src 'self'` and scoped `form-action`, no `script-src` | locally served consent CSS; no script / CDN style dependency |
| SeerrSense public and other error routes | effective/specific policy **unknown (not observed)**; public page contains an inline theme script and local `site.js` | audit server headers and edge response before proposing any CSP hardening |

**Remaining gate:** read-only effective HTTP response headers on representative public, consent/auth, error and redirect routes (only where safe and not involving secrets); verify CDN `Cache-Control`, `Access-Control-Allow-Origin`, and optional SRI feasibility; test responsive navigation with Playwright/axe in a preview. None of these were executed as part of the research-only AMEND. The mctl.ai performance baseline remains `not-run`, and migrations are **not** justified by current evidence.
