# Spike: provider-neutral security finding lifecycle (D1-D7)

Status: report for review by the owner of mctlhq/.github#209. Read date: 2026-10-23.

## Header and non-claims

- Output of the spike in mctlhq/.github#209. Nothing is rolled out. No roadmap manifest,
  issue, runtime component or gitops state is changed by this report.
- `docs/spikes/security-finding-lifecycle/model.py` is a **non-normative** reference state
  machine on synthetic fixtures. It is not a service and not the canonical schema. D4 is a
  candidate for review by the owner repos.
- Tags: `[I]` inferred by the author from the cited source, `[P]` stated by a public
  document. Anything not observed is `unknown (not observed)`.
- Claude Security live access: **none**. It is mapped from the public article (fetched
  2026-10-23) only. No API field or webhook event names are published, so its fixture is
  labelled hypothetical.
- Only identifiers, digests and public CVE ids are cited. No code bodies or payload text.
- The recommended roadmap action in D7 is text only.

## 1. Evidence pins

| Repository | Commit (40 hex) | Read date | What was read |
|---|---|---|---|
| mctlhq/.github | 6bc591be90ac421e07f5c67dace7029722cfe5a3 | 2026-10-23 | `AGENTS.md`, `roadmap/epics/agent-runtime-assurance.yaml`, `events/README.md`, `.github/workflows/claude-review.yml`, `agy-review.yml`, `dependabot.yml` |
| mctlhq/mctl-api | 2b6d2eaff15276dd7e4458b51fad160dc32d333e | 2026-10-23 | `.github/workflows/security.yml`, `claude-review.yml`, `agy-review.yml` |
| mctlhq/mctl-agents | f86ab74072dacb7b2091b80cad118a2eca86d6c9 | 2026-10-23 | `orchestrator/runtime_assurance.py`, `docs/adr/022-runtime-assurance-contract.md`, `tests/fixtures/assurance/finding-confirmed.json`, `.github/workflows/security.yml`, `pr-validation.yml` |
| mctlhq/mctl-gitops | e799949d3d83f01603f0ec9874210868dcc0e894 | 2026-10-23 | `platform-gitops/agents-state/` (CVE measurements), `OPERATOR.md` |

Shorthand: `api@2b6d2ea`, `agents@f86ab74`, `gitops@e799949`, `dot@6bc591b`.

Not read: mctl-agent and mctl-portal workflows. Their Trivy/CodeQL setup is
`unknown (not observed)`.

## 2. D1 - current-state map

Ten Q1 attributes per source: identity, dedup key, severity, state set, fix binding,
revision binding, verification, reopen, dismissal, owner.

| Source | Findings (what exists) | Observed lifecycle |
|---|---|---|
| GitHub code scanning / CodeQL | Not observed in mctl-api or mctl-agents workflows (`api@2b6d2ea:.github/workflows/security.yml` runs Trivy). Provider-native model [P]: alert number per repo plus rule id; states open/dismissed/fixed; webhook actions include created, fixed, reopened, closed_by_user, appeared_in_branch. | Native fixed/reopen, per provider, not bound to MCTL execution evidence. Dismissal reason/actor [P]. Other attributes: unknown (not observed) for MCTL repos. |
| Trivy CI | `api@2b6d2ea:.github/workflows/security.yml` and `agents@f86ab74:.github/workflows/security.yml`, weekly cron plus runs on `aquasecurity/trivy-action`. | Output is CI log/SARIF at most. Identity beyond the run: unknown (not observed). No dedup, no state, no reopen. |
| Dependabot | `dot@6bc591b:.github/dependabot.yml` covers `github-actions` updates only. | Dependabot alerts for other repos: unknown (not observed). |
| Claude / agy PR review | `.github/workflows/claude-review.yml`, `agy-review.yml`; P1/P2/P3 inline comments bound to `commit_id`. | Identity is the comment. No dedup, no lifecycle after merge, no verification. |
| agents-state CVE proposals | `gitops@e799949:platform-gitops/agents-state/`: 156 `requirements.md` cite a CVE id; 6 `mctl-api` proposals target CVE-2025-54236; 13 target CVE-2026-32283; 9 target CVE-2026-24046. | Only `pgx-sqli-cve-2025-54236` has a `.status.yaml` (`status: merged`, `pr: mctl-api#40`); it holds no verification field although the requirements demand a zero-findings scan. `OPERATOR.md` lifecycle ends at merged/rejected: no verified or reopened state. |
| Manual GitHub issues | Free-form. | Identity is the issue number. No machine dedup key. |
| Runtime Assurance `AssuranceFinding` | `agents@f86ab74:orchestrator/runtime_assurance.py`, fixture `finding-confirmed.json`: `finding_id`, `state` (confirmed), `invariant_id`, binding of scenario, runtime versions and target `revision`, `validation` with replay run. | Agent/MCP boundary scope only. Deterministic replay validation exists. No CVE/scanner semantics. The roadmap still reports the #151 work items as unbound. |

Summary: at least six finding representations, with `AssuranceFinding` as a seventh. None
shares a dedup key. All except provider-native alerts end at "PR merged", and none records
verification against the landed revision. **D1 confirms the gap.** Per-cell detail for the
GitHub column of unobserved attributes remains `unknown (not observed)`.

## 3. D2 - provider matrix (12 rows)

| # | Attribute | GitHub / CodeQL [P] | Claude Security (public article, no live access) | Runtime Assurance (`agents@f86ab74`) |
|---|---|---|---|---|
| 1 | Stable id | alert `number` per repo | unknown (not published) | `finding_id` (`afd-...`) |
| 2 | Rule / class id | `rule.id` | unknown | `invariant_id`, `scenario_id` |
| 3 | Dedup key | repo + alert number; rule + location | continuous rescans merge findings [P]; key unknown | content hash of finding |
| 4 | Severity | `rule.severity` | unknown | none; verdict-based |
| 5 | States | open, dismissed, fixed | fixed, reopened on re-detection [P] | candidate, confirmed, rejected |
| 6 | Revision binding | `most_recent_instance.commit_sha`, `ref` | unknown | `binding.target.revision` |
| 7 | Fix marker | `fixed_at` | marked fixed when the fix ships [P] | `supersedes` |
| 8 | Verification | rescan by analysis | rescan [P] | replay (`validation.replay_run_id`) |
| 9 | Reopen | `reopened` webhook action | auto-reopen on re-detection [P] | new finding supersedes |
| 10 | Dismissal | `dismissed_reason`, `dismissed_by` | resolve via API [P]; reasons unknown | rejected state |
| 11 | Delivery | webhooks with `X-GitHub-Delivery` | webhook on new findings only [P] | internal |
| 12 | Auth | token/app | personal access token with user permissions [P] | internal |

## 4. D3 - Runtime Assurance boundary

Chosen option: **reference**. `SecurityFinding` references `AssuranceFinding`; a confirmed
`AssuranceFinding` is one provider (`provider=mctl-assurance`) with verification strength
`deterministic_replay`. Extending `AssuranceFinding` is rejected. Preserved #151 success
criteria: a model-reported finding is never accepted as ground truth without reproducible
validation; findings bind to exact scenario, runtime and target versions; scope stays
agent, MCP and tool execution boundaries.

## 5. D4 - candidate contract

Gap confirmed in D1, so a candidate is given (non-normative).

- Identity: `sf_` id. Canonical key = hash(vuln class id, repository identity, component).
  Observations keyed by (provider, delivery id), following `events/README.md`.
- References only: repository identity, vulnerable and fixed revision (immutable SHA),
  evidence refs, `work_item_id`. Provider severity, title and description are untrusted
  evidence and never an MCTL policy or authorization input.
- States and transition owners:

| Transition | Owner | Required evidence |
|---|---|---|
| (new) to open | MCTL, from observation | provider observation |
| open to triaged | human | actor |
| triaged to fixing | MCTL | one WorkItem reference (one per canonical key) |
| fixing to awaiting_verification | MCTL | merged fix revision |
| awaiting_verification to resolved | MCTL | result `absent` bound to the landed revision, strength at least policy minimum (default `scanner_rerun_exact_revision`) |
| awaiting_verification to unverifiable | MCTL | error, unavailable or timeout result |
| resolved to reopened | MCTL | two consecutive regression observations, or one deterministic test, on a descendant revision |
| any to dismissed(reason) | human | actor, authority reference, reason in false_positive, accepted_risk, duplicate, not_applicable, provider_withdrawn |

- Strength order: deterministic_regression_test > assurance_replay >
  scanner_rerun_exact_revision > provider_claim > manual.
- A dismissed finding plus regression yields a linked successor, not a reopen.
- Surface (text only): `list`, `get`, `start_remediation`, `request_verification`,
  `dismiss`. No generic set-state.

## 6. D5 - experiment results

`python3 -m unittest discover -s docs/spikes/security-finding-lifecycle/tests` runs 10 tests,
all passing, covering: full path with reopen under the same `sf_` id; duplicate delivery
(1 finding, 1 observation, 1 remediation); cross-provider dedup with a second remediation
refused; merged is not resolved; error/unavailable gives unverifiable; verification must
match the landed revision; provider `fixed` claim does not resolve; dismissal requires actor,
authority and a known reason; regression after dismissal yields successors only; flapping
guard; and hostile provider text causing no transition. Run against the commit that carries
this report. Fixtures are synthetic and shaped from public docs, the Claude fixture is
hypothetical.

## 7. D6 - ownership map (text only)

| Item | Repo | Action | Priority | Depends on |
|---|---|---|---|---|
| Durable store and API for findings | mctl-api | create | high | D4 review |
| Remediation hookup and verification runner | mctl-agents | extend | high | store |
| Webhook endpoint config and credentials | mctl-gitops | extend | medium | store |
| Runtime Assurance | mctl-agents (#151) | unchanged | none | none |

No new epic and no roadmap manifest change.

## 8. D7 - recommendation

Decision rule: `NO_NEW_CONTRACT` if provider-native lifecycles plus issue automation already
give dedup and verified closure for every source. `CREATE_SECURITY_EPIC` if more than about
three independent cross-repo slices are needed. Otherwise `ADD_SECURITY_FINDING_CONTRACT`.
D1 shows no shared dedup key, no verified closure and no reopen path outside provider-native
alerts, and D6 needs three slices.

Recommendation: **ADD_SECURITY_FINDING_CONTRACT**

Limitations: Claude Security is unverified, Trivy/CodeQL setup in mctl-agent and mctl-portal
was not read, and the mctl-agents#694 shape is read only as far as the pinned fixtures.
