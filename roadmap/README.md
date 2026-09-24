# Declarative roadmap control plane

This directory is the Git source of truth for mctl roadmap **desired state**.
GitHub Issues/Projects remain the human collaboration surface; a future reconciler
compares their observed graph with these manifests and, only after read-only drift
validation is proven, applies governed changes.

The first resource is:

```yaml
apiVersion: roadmap.mctl.ai/v1alpha1
kind: EpicDefinition
```

Parent initiative: `mctlhq/.github#66`.

## Why this exists

An epic graph currently has several representations: issue prose, native sub-issues,
`Depends on` sections, Project fields, and comments. Reconstructing the graph from
those sources repeatedly makes drift inevitable and makes critical-path reasoning
non-deterministic.

`EpicDefinition` follows the same mctl pattern as `AgentDefinition` and
`ReleaseBindingIntent`: reviewed, versioned desired state in Git; deterministic
validation; observed state kept separately.

## Source-of-truth rules

One relation has one authored direction:

- `parent` is authored; `children` is derived.
- `dependsOn` is authored; `blocks` is derived.
- GitHub issue state/progress is observed; it is not copied into the manifest.
- critical path, ready/blocked counts, drift and completion percentage are derived.

Hierarchy and dependency are deliberately separate. A child does not implicitly
block its parent, and phase order does not create dependency edges.

The v1alpha1 completion rule is intentionally small: `allRequired` means every
`workItem.required: true` must reach its terminal completion condition in the
observed GitHub/read model. Optional items do not block epic completion.

## Layout

```text
roadmap/
  README.md
  requirements.txt
  schemas/
    epic-definition.schema.json
    github-graph-snapshot.schema.json
    roadmap-diff.schema.json
    roadmap-health.schema.json
    roadmap-ready-set.schema.json
    roadmap-apply-plan.schema.json
    roadmap-apply-result.schema.json
    roadmap-publication.schema.json
  epics/
    human-input.yaml
  fixtures/
    human-input/
      converged-fixture.json
  scripts/
    validate.py
    github_graph.py       read-only observation
    reconcile.py          desired vs observed
    health.py
    completion.py
    ready.py              dependency-aware readiness
    plan.py               diff -> the mutations a manifest authorizes
    github_apply.py       the only module that may write
    apply.py              execute a plan, emit an audited result
    publish.py            one RoadmapPublication for the roadmap-state branch
    publication_request.py  ask roadmap-publish.yml for a fresh publication
    publication_order.py    monotonic capturedAt and scheduled-skip guards (stdlib only)
  publication/
    README.md             copied onto roadmap-state as its README
  tests/
    test_validate.py
    test_reconcile.py
    test_health.py
    test_completion.py
    test_ready.py
    test_plan.py
    test_apply.py
    test_publish.py
    test_publication_freshness.py
    mutations.py
```

`human-input.yaml` is the first real pilot because it is cross-repository, already
well decomposed, and has both required and optional work plus an external dependency.
Enterprise MCP is intended to be the next, more complex migration after the model is
proven.

## Validation

Create a virtual environment and run:

```bash
python -m pip install -r roadmap/requirements.txt
python roadmap/scripts/validate.py roadmap/epics
python -m unittest discover -s roadmap/tests -p 'test_*.py'
```

The same checks are enforced by `.github/workflows/roadmap-validate.yml` for roadmap
changes, with no write permissions.

The validator has three layers:

1. JSON Schema checks the versioned structural contract and rejects undeclared fields
   such as authored `blocks`/`children`/`status`.
2. Per-manifest semantic validation checks graph invariants that JSON Schema cannot
   express cleanly: unique phase/work-item IDs, valid local references, acyclic parent
   and dependency graphs, unique local GitHub bindings, external dependencies that
   truly point outside the epic, and enough metadata for unbound future work.
3. Corpus validation checks invariants across all `roadmap/epics/*` manifests,
   including unique epic names and globally unique GitHub issue bindings.

The validator is intentionally offline and read-only. It does not query or mutate
GitHub.

## v1alpha1 model

An epic owns a GitHub parent issue and a set of work items. A work item may either be
bound to an existing GitHub issue or remain unbound as desired future work. An
unbound item must carry `title` and `owner` so a later governed reconciler has enough
intent to propose/create the issue.

Local `dependsOn` edges reference work-item IDs in the same manifest.
`externalDependsOn` records an existing GitHub issue outside this epic without
pretending that the external issue is owned by this manifest. If that issue is bound
inside the same manifest, the validator requires the local `dependsOn` form instead.

Nested issue decomposition is represented with `parent`, which references another
local work-item ID. Omitting `parent` means the work item is a direct child of the
epic root for reconciliation purposes.

## Reconciliation

`reconcile.py` compares one or more manifests with an observed GitHub graph and emits
a `RoadmapDiff`. It never mutates anything.

```bash
# offline: replay the synthetic converged fixture
python roadmap/scripts/reconcile.py roadmap/epics/human-input.yaml \
  --corpus roadmap/epics \
  --snapshot roadmap/fixtures/human-input/converged-fixture.json

# live: GET-only read of the real graph (needs GITHUB_TOKEN or GH_TOKEN)
python roadmap/scripts/reconcile.py roadmap/epics/human-input.yaml --live

# live plus an immutable capture of exactly what was read
python roadmap/scripts/reconcile.py roadmap/epics/human-input.yaml \
  --capture roadmap/fixtures/human-input/live-capture.json
```

Exit codes: `0` converged, `1` drift, `2` usage/IO/auth/validation error.

One manifest produces a single `RoadmapDiff`. Several produce a `RoadmapDiffList`
envelope, items ordered by manifest path. Both are defined in
`schemas/roadmap-diff.schema.json`, so every output validates against the published
contract -- never a bare JSON array with no `kind`.

**Unobserved or unreadable state must never be projected as absence.** The reader
distinguishes "observed absent" from "could not observe", and only three responses
mean absent:

| request | absent means |
| --- | --- |
| `GET .../issues/{n}` | 404 or 410 — the issue does not exist |
| `GET .../issues/{n}/parent` | 404 — the issue has no parent |
| `GET .../sub_issues`, `.../dependencies/blocked_by` | `200 []` — no relations |

Everything else is an observation error (exit 2), never an empty relation set: a 404 or
410 on a relation listing, a later page failing after earlier pages succeeded, a 410 on
`/parent` for an issue that was just read, or any body that is not the expected shape.
A live capture is also validated against the snapshot schema before it is returned, so
the producer is held to the same contract replay loads it under.

### Validation preflight

Positional manifests select what is *diffed*. They never narrow what is *validated*:
the entire canonical corpus under `--corpus` (default `roadmap/epics`) is schema-,
semantic- and corpus-validated first, and any failure exits 2 with zero network calls.
Otherwise reconciling one file could pass while another manifest silently claims the
same GitHub issue, and ownership that survives validation would be contradicted by the
live graph.

### Read-only by construction

The live adapter uses four documented REST endpoints and nothing else:

```text
GET /repos/{owner}/{repo}/issues/{number}
GET /repos/{owner}/{repo}/issues/{number}/parent
GET /repos/{owner}/{repo}/issues/{number}/sub_issues
GET /repos/{owner}/{repo}/issues/{number}/dependencies/blocked_by
```

Every request funnels through one helper that raises `WriteAttempted` on a non-GET
method or a request body *before* transmission. GraphQL is deliberately unused: the
safety property of this slice is that read-only is checkable by construction, not
promised by convention. CI never runs live mode.

### Diff types

| family | type | meaning | severity |
| --- | --- | --- | --- |
| binding | `BindingIssueNotFound` | the bound issue could not be resolved | drift |
| binding | `BindingRedirected` | the issue answers under another canonical identity | drift |
| binding | `BindingAmbiguous` | the endpoint is observable under more than one parent | drift |
| binding | `BindingUnbound` | authored work with no issue yet | informational |
| hierarchy | `HierarchyMissingParent` | expected parent edge absent | drift |
| hierarchy | `HierarchyWrongParent` | the child sits under a different parent | drift |
| hierarchy | `HierarchyUnexpectedChild` | observed child this manifest does not own | informational |
| dependency | `DependencyMissing` | authored blocked-by edge absent | drift |
| dependency | `DependencyUnexpected` | observed blocked-by edge nobody authored | drift |

Informational entries are always emitted and never change exit 0 on their own. They
are informational in severity, not optional in emission.

### Redirect and suppression

A transferred issue is *binding* drift, not relation drift. It resolves to a real
canonical identity, so exactly one `BindingRedirected` is emitted, relation endpoints
are rewritten onto the resolved key, and comparison continues. Requested identity is
recorded from the request URL before transmission and resolved identity from the
response body, because an HTTP client may follow the 301 itself and leave the final
URL useless as evidence.

Only endpoints that could not be pinned down -- unresolvable or ambiguous -- suppress
the relations that touch them, and suppression holds in both directions: a withheld
endpoint never comes back as an "unexpected" relation on its neighbour. Otherwise one
ambiguous binding would cascade into drift on every issue pointing at it.

`BindingAmbiguous` also covers the reverse collision: when two *owned* bindings
resolve to the same canonical issue -- which a transfer can cause, and which the
offline validator cannot see because the authored identities differ -- both are
reported and both are suppressed. Otherwise one live object would quietly satisfy two
work items.

An `externalDependsOn` reference is an endpoint, not a binding. It names somebody
else's issue, so it never participates in that collision rule: two external references
landing on one issue says nothing about this epic, and letting them collide would
suppress a perfectly good binding on the strength of an outside dependency. Several
work items may name the same external issue; it is still one issue, so it is settled
once and attributed to the first work item that referenced it rather than producing
one entry per dependent item.

An issue the snapshot never observed is an error, not a `BindingIssueNotFound`.
Reporting "not found" for something nobody looked at would invent evidence. The same
rule applies one level down: a `found` observation must carry `parent`, `subIssues`
and `blockedBy`, so a relation nobody fetched can never read as an observed absence.
A snapshot may also observe any issue at most once, since two entries for one request
would make normalization depend on input order.

`capturedAt` and `updatedAt` are checked as real RFC 3339 timestamps rather than left
to the schema's `format` annotation, which asserts nothing on its own. Evidence that
claims a time nobody can parse is not evidence.

`epic` is reserved as a work-item id: it is the owner name the root binding reports
under, in validator diagnostics and in every `RoadmapDiff` entry.

### Determinism

`RoadmapDiff` carries the SHA-256 of the exact manifest bytes, the manifest path
relative to the repository root, and the snapshot's own provenance. It adds no
timestamp or random value of its own -- the only time a diff carries is a live
capture's `capturedAt`, copied from the snapshot as provenance -- so identical
manifest and snapshot bytes produce byte-identical JSON on any machine, in any input
order.

### Fixtures

Two different classes of artifact, never interchangeable:

- `source.mode: live-capture` with `capturedAt` and `apiBase` -- immutable evidence of
  a real read. Never hand-edited to make a test pass.
- `source.mode: synthetic-fixture` with optional `derivedFrom` -- the green test graph.
  It may be derived from a capture but may never claim a capture timestamp.

The schema enforces the distinction, so a fixture cannot quietly promote itself to
evidence.

### Mutation testing

`tests/mutations.py` holds pure deep-copy mutators for parent edges, dependency edges,
bindings, redirects and ambiguity. The suite proves the detector in both directions:
the converged fixture is green, each single mutation turns exactly one expected entry
red without cascading into a neighbouring family, and restoration returns to green.
A detector that can only report drift is as broken as a guard that can only pass.

## Health

`health.py` turns a reconciliation run into a computed `RoadmapHealth` per epic: a state
and the diagnostics that produced it, so a consumer can say *whether* an epic is healthy
and *why* without re-running reconciliation.

```bash
python roadmap/scripts/health.py roadmap/epics/human-input.yaml \
  --snapshot roadmap/fixtures/human-input/converged-fixture.json
```

| state | meaning | exit |
| --- | --- | --- |
| `healthy` | observed, valid, zero drift | 0 |
| `drift` | observed and valid, at least one drift entry | 1 |
| `invalid` | the authored desired state (manifest or corpus) did not validate; nothing was observed | 3 |
| `observation_failed` | some state this epic depends on could not be observed | 4 |

Exit `2` stays a usage error. With several epics the output is a `RoadmapHealthList` and
the exit code is the most severe state. Both shapes are in
`schemas/roadmap-health.schema.json`.

Precedence, most severe first:

```text
observation_failed > invalid > drift > healthy
```

The invariant:

```text
Observed absence != unobservable state.

A missing relationship may be projected as absent only when the
authoritative source was successfully observed.
```

So:

- `healthy` requires a successful observation. Evaluating with no observation at all is
  `observation_failed` (`ObservationAbsent`), never a vacuous `healthy`.
- A snapshot that never observed some bound issue does not fail the whole run. Those
  endpoints are withheld from comparison and reported as `ObservationMissing` errors;
  drift on the observed remainder is still carried as diagnostics, and the state is
  `observation_failed`, not `drift`. No absence entry is ever produced for an unobserved
  endpoint.
- An unusable snapshot (`SnapshotInvalid`) or an unreadable source (`ObservationFailed`) is
  an observation failure. `invalid` is reserved for the authored desired state.
- A more severe state never discards less severe evidence: every validation error,
  observation failure and diff entry stays in `diagnostics`.

Diagnostics carry a stable `code` (a `RoadmapDiff` entry type, or one of `ObservationFailed`,
`ObservationMissing`, `ObservationAbsent`, `SnapshotInvalid`, `ManifestInvalid`,
`CorpusInvalid`), a `level` (`error`, `drift`, `info`) and structured `subject` evidence.
Evaluation is pure and deterministic: no network I/O and no clock.

`UNEXPECTED_SILENCE` from #56 is deliberately not a state here. It needs liveness windows
and a notion of "now", which conflicts with deterministic evaluation of one snapshot; it is
tracked as a follow-up.

## Completion

`allRequired` is computed, not declared. Every `RoadmapHealth` built from an observed
snapshot carries a `completion` block:

```json
"completion": {
  "mode": "allRequired",
  "status": "incomplete",
  "required": {"total": 3, "complete": 2, "incomplete": 1, "unknown": 0},
  "blocking": ["governed-apply"],
  "items": [{"id": "temporal-health", "required": false, "status": "incomplete", "reason": "open", ...}]
}
```

Per work item:

| status | reason | when |
| --- | --- | --- |
| `complete` | `closed` | issue closed as `completed`, or closed with no recorded reason |
| `unknown` | `closed_reason_unrecognized` | closed with any other reason (e.g. `reopened`, or a value GitHub adds later) — fail closed, never counted as done |
| `incomplete` | `open` | issue open |
| `incomplete` | `closed_not_planned`, `closed_duplicate` | closed without delivering the work |
| `incomplete` | `unbound` | no GitHub issue yet |
| `incomplete` | `issue_not_found` | observed, and the issue does not exist |
| `unknown` | `unobserved`, `state_not_observed` | the state could not be observed |

Epic status looks at required items only: `incomplete` if any was observed incomplete,
otherwise `unknown` if any could not be observed, otherwise `complete`. Optional items
(`required: false`) are always listed and never appear in `blocking`.

Completion is a separate axis from health — an epic can be `healthy` and `incomplete`, and
it does not change the CLI exit code. An item nobody could observe is `unknown`, never
`incomplete` and never `complete`. Snapshots carry GitHub's `stateReason` so closed-as-done
can be told apart from closed-as-not-planned; the reason is kept verbatim, so an unrecognised
value surfaces as `unknown` instead of being dropped and read as delivered.

## Readiness

`ready.py` answers a different question than completion: not "is this item done"
but "which bound work items are executable right now." It joins each work item's
authored `dependsOn` / `externalDependsOn` predecessors with `completion.item_status`
-- the same completion axis `health.py` computes, never a second interpretation of
GitHub state -- and emits a `RoadmapReadySet`:

```bash
python roadmap/scripts/ready.py roadmap/epics/human-input.yaml \
  --corpus roadmap/epics \
  --snapshot roadmap/fixtures/human-input/converged-fixture.json
```

Per work item, one of four states:

| state | meaning |
| --- | --- |
| `complete` | the item's own bound issue is delivered |
| `ready` | the item's own issue is `open`, and every authored predecessor is complete |
| `blocked` | the item is itself incomplete, and non-readiness is evidenced: a predecessor was observed incomplete (`open`, `closed_not_planned` or `closed_duplicate`), or the item's own issue is closed as `not_planned`/`duplicate` |
| `unknown` | the item's own readiness could not be proven, or a predecessor's could not |

`blocked` beats `unknown` when both apply to the same item: non-readiness is
already proven evidence, the same certainty-first precedence `completion.compute`
uses when it prefers `incomplete` over `unknown`. Every non-satisfied predecessor
is still listed in `blockers`, so nothing is hidden either way.

Unbound and not-found predecessors are `unknown`, not `blocked`: `completion.py`
calls both `incomplete` because they are evidence of undelivered work, but
`ready.py` only lets `open`, `closed_not_planned` and `closed_duplicate` make a
*dependent* item `blocked` -- an unbound or not-found predecessor means readiness
cannot be proven, not that it is definitely still open. This is a readiness-layer
refinement; it changes nothing in `completion.py`.

An `externalDependsOn` reference is evaluated with the same rules as a bound work
item, from the same captured snapshot, and appears in `blockers` as
`{"kind": "external", "issue": {...}, ...}` rather than a synthetic local id. If
its completion cannot be proven from the snapshot, the dependent item is `unknown`,
never `ready`.

Invariants:

- Dependencies are derived only from the manifest's authored `dependsOn` and
  `externalDependsOn` -- never from GitHub labels, issue prose, observed
  `blockedBy` edges or `parent`/`subIssues` hierarchy. `reconcile.py`'s
  `DependencyMissing` diagnostic is what tells you the observed graph disagrees
  with the manifest; `ready.py` never falls back to the observed graph itself.
- Only one hop over the authored graph is evaluated: `validate.py` already
  rejects dependency cycles, and a `complete` predecessor's own predecessors say
  nothing about this item.
- An unbound work item is always `unknown`, never `ready` -- the strongest safety
  property for a wave launcher: `state: ready` implies a bound, observed, `open`
  issue.
- `ready` is narrower than "own issue not complete". `closed_not_planned` and
  `closed_duplicate` are evidence of undelivered work when a *predecessor*
  carries them, but on the item's own issue they mean GitHub has already retired
  it, so the item is `blocked`, never `ready` -- otherwise a wave launcher would
  be handed an issue that is already closed. The asymmetry is deliberate: it is
  the one place where an item's own reason and a predecessor's are read
  differently. Such an item is the only case where `blockers` names the item
  itself; every other blocker is a predecessor.
- `required: false` items still get a readiness state, and still count as real
  predecessors of anything that names them in `dependsOn`.
- No snapshot at all: no `RoadmapReadySet` is emitted, the failure goes to
  stderr, exit `4`. A partial snapshot still emits a document -- the affected
  items are `unknown`, never silently absent -- and still exits `4`.
- Readiness never changes `RoadmapHealth` state, `completion` block, diagnostic
  code or CLI exit code of `reconcile.py`, `health.py`, `plan.py` or `apply.py`.
  It is a new file, not an edit to an existing contract.

CLI, mirroring `reconcile.py` / `health.py`: positional manifests, `--corpus`,
`--schema`, `--ready-schema`, `--snapshot`, `--live`, `--capture`, `--api-base`,
`--output`; `--snapshot` and `--live`/`--capture` stay mutually exclusive, and the
whole corpus is validated before any network call.

| exit | meaning |
| --- | --- |
| `0` | a ready set was produced (an epic with zero ready items is not an error) |
| `2` | usage/IO error |
| `3` | the authored desired state (manifest or corpus) did not validate; nothing was observed |
| `4` | some state this epic depends on could not be observed, in whole or in part |

`ready.consistency_errors(document)` is the semantic check to run on a
`RoadmapReadySet` a caller did not compute itself, the same way
`completion.consistency_errors` is run on a completion block: it rejects a `ready`
item with blockers, a `ready` item whose own reason is not `open`, a `blocked`
item with no blocker whose reason is evidence of undelivered work, a `complete`
item with any blocker, an `unknown` item with neither an indeterminate own reason
nor an indeterminate blocker, a `ready` list that is not exactly the sorted ready
ids, summary counts that disagree with `items`, a `workItem` blocker naming an id
that is not part of the same manifest, or an item naming itself as a blocker for
any reason other than its own retirement.

`roadmap/tests/mutations.synthetic_snapshot()` builds a converged graph for any
manifest directly from `reconcile.desired_graph()`, so `lifecycle-ownership` and
`unified-identity` -- which have no committed capture -- get a graph to test
readiness against without hand-editing one. It always stamps
`source.mode: synthetic-fixture`, so a test graph can never masquerade as live
evidence.

## Publication

`.github/workflows/roadmap-publish.yml` is the one place the evaluator runs against live
GitHub (on every `roadmap/**` change on `main`, on request, and as an hourly
reconciliation -- see [Publication freshness](#publication-freshness)). A read-only `build` job installs the evaluator, observes and verifies; a
`publish` job that installs nothing and holds the only write token pushes exactly one
commit to the orphan branch `roadmap-state`, holding one `RoadmapPublication` (#119):

| File | Content |
|---|---|
| `snapshot.json` | one GET-only capture over the union of every manifest's issues |
| `ready-set.json` | `RoadmapReadySetList`: `ready.py` for every manifest, replayed from that snapshot |
| `health.json` | `RoadmapHealthList`: `health.py` for every manifest, replayed from that snapshot |
| `publication.json` | evaluator and source revision, manifest sha256s, the observation, file sha256s |

```bash
# what the workflow runs; --snapshot instead of --live replays a fixture
python3 roadmap/scripts/publish.py build --live --output out/ \
  --evaluator-revision "$(git rev-parse HEAD)" --source-repository mctlhq/.github \
  --source-ref main --source-revision "$(git rev-parse HEAD)"
# recompute a publication from its own snapshot at the recorded revision
python3 roadmap/scripts/publish.py verify out/
```

Rules the tests pin:

- **Generated state, not a source.** Nothing on `roadmap-state` is edited by hand or read
  back as desired state; the manifests are never written.
- **One observation.** All three derived files come from the snapshot in the same
  commit, so they cannot disagree about what GitHub looked like.
- **Freshness is `observation.capturedAt`.** There is no wall clock in the output: the
  same manifests and snapshot give the same bytes. Every live capture has its own
  `capturedAt`, so every successful run commits. A `synthetic-fixture` observation has no
  `capturedAt`.
- **Always a List.** Both derived files are the List form even for a one-manifest corpus
  (the schemas allow a one-item List), so the kind never changes with corpus size.
- **Fail closed.** An invalid corpus (exit 3), a repository the token cannot see or whose
  Issues are turned off (`has_issues: false`, which also refuses a fork whose issues would
  read fine: the safe default), a failed or incomplete observation, or an unobserved key
  (exit 4), or a publication that does not reproduce from its own
  snapshot fails the run before anything is pushed. The previous publication stays, with
  its older `capturedAt`, so a failed refresh never looks fresh; the failed run is the
  visible signal.
- **Unbound is not unknown.** Both reach consumers as `unknown`, distinguished by
  `completion.reason: unbound`.

Consumers (mctl-api#333) read these files; they never run the evaluator or infer
readiness themselves.

### Publication freshness

mctl-api plans and executes a wave only from a publication whose `capturedAt` is at
most `ROADMAP_WAVE_MAX_AGE` old (default 30 minutes, not overridden in mctl-gitops).
The graph moves for three kinds of reason, and each has its own path to a new
publication:

```text
manifest or evaluator merged to main ---- push -----------------\
governed apply that landed a write ------ workflow_dispatch -----+--> roadmap-publish.yml
operator about to plan a wave ----------- workflow_dispatch ----/          ^
issue closed/reopened anywhere else ----- (no event reaches us)            |
                                          hourly reconciliation -----------/
```

- **`push`** runs on every `roadmap/**` change on `main`, as before.
- **After a governed apply.** `apply.py --live --execute` requests a publication
  (`publication_request.py`: one `workflow_dispatch` of `roadmap-publish.yml` on `main`,
  the only request that module can send) when at least one write landed -- including
  a run that stopped part-way, because its landed writes already changed the graph. A
  plan-only run, a replay, and a live run whose every operation was already satisfied,
  skipped or failed request nothing. `--no-publication-request` opts out. A request that
  fails is a `WARNING` on stderr and leaves the exit code to the writes; the
  publication then stays exactly as old as its `capturedAt` says. A run interrupted
  after a write landed sends nothing from the interrupt but prints the same warning.

  The request needs **`actions: write` on `mctlhq/.github`** (a fine-grained token's
  "Actions: read and write", or a classic token's `repo` scope), which is more than the
  relation writes need. `apply.py` sends it with the token it already reads
  (`GITHUB_TOKEN`/`GH_TOKEN`). A token without that scope gets a 403, and every apply
  then prints the `WARNING` and requests nothing, so a repeated warning means the token
  needs the scope. It does not mean the publisher is broken.
- **Before a wave.** No schedule can promise 30 minutes (below), so whoever is about to
  plan a wave asks first and waits for `capturedAt` to move:

  ```bash
  GITHUB_TOKEN=... python3 roadmap/scripts/publication_request.py
  # equivalently: gh workflow run roadmap-publish.yml --repo mctlhq/.github --ref main
  ```

- **Hourly reconciliation** (`17 * * * *`) is the safety net for graph changes that no
  event reports, such as an issue closed by a merge in another repository.

Every run first decides whether a capture would add anything (`publication_order.py
decide`, tested against a real git history). The publisher's inputs count as unchanged when every `roadmap/**` file and the
workflow are byte-equal to the published source revision; a commit elsewhere on `main`
cannot change what is published. With unchanged inputs:

- a **scheduled** run skips while the capture is at most 90 minutes old;
- a **pushed or dispatched** run skips when the published capture started strictly after
  the run was created. Whatever asked for the run happened before that, so the capture
  already saw it. Runs are serialized and GitHub keeps one pending run per group, so a
  burst of applies costs one or two captures, not one per apply.

The cron is a net, not the freshness contract. GitHub runs schedules best effort: on
2026-09-23, between the workflow's merge at 08:51Z and 23:00Z, seven `17 */2` slots
passed and three scheduled runs started, at 15:02Z, 19:41Z and 22:57Z -- 40 to 85
minutes after the nearest slot, the rest not at all. A 30-minute cron would still miss 30 minutes, and would cost more than the
budget allows:

| | GETs |
|---|---|
| one capture (`publish.py cost`) = repositories + 4 x issues (issue, `/parent`, `/sub_issues`, `/dependencies/blocked_by`) | 9 + 4 x 134 = **545** on 2026-09-23 |
| `GITHUB_TOKEN` primary budget, per repository per hour (GitHub docs; the planning basis) | 1000 |
| what `GET /rate_limit` reported to the build job on 2026-09-24 (run 35939562422) | 5000, and still 5000 after a 549-GET capture |
| a cron every 30 minutes | 1090 / hour -- over budget before any event |
| hourly net, captured every other hour in a quiet period | ~273 / hour on average |

So at most one capture fits in any one hour window, whatever triggered it. The build job
therefore reads the token's real budget (`GET /rate_limit`, which is free) and never
starts a capture it cannot finish: one that runs out part-way publishes nothing and
spends the budget the next run needs. A scheduled run that is short skips, because the
next tick is as good. A pushed or dispatched run waits for the reset, holding the
concurrency slot, so requests that arrive meanwhile coalesce into the single pending run
behind it. It polls at most once a minute and waits at most 75 minutes (two reset
windows; `BUDGET_WAIT_SECONDS` in `publication_order.py`, which a test holds this
sentence to). After that it leaves a `::warning::` and hands the publication to the
next scheduled run that finds budget, rather than being killed silently by the job
timeout. An unreadable budget -- a failed read, or anything other than three
integers -- is read at most 3 times in a row (`BUDGET_READ_ATTEMPTS`) before the run
gives up the same way.

The step logs the budget it saw before and after the capture. The first logged run
(2026-09-24) saw 5000/5000 both times, so `/rate_limit` does not show what a capture
spends. The preflight still refuses a budget it can see is short. The plan above keeps
the documented 1000, because it is the lower figure, and it does not rely on the reading
going down.

Two guards keep a publication from ever looking fresher than its observation:

- **Monotonic.** Runs are serialized (`concurrency: roadmap-publication`), but a queued
  run can hold a capture that a later-started run already published past. The publish
  job pushes only a strictly newer `capturedAt` (`publication_order.py newer`); an
  overtaken capture is a notice, not a push, and an invalid one fails the run.
- **Never forced.** A rejected push fails the run, as before.

## Dogfood: epic #66

`epics/roadmap-control-plane.yaml` is the canonical `EpicDefinition` for this control plane
itself (`mctlhq/.github#66`): the reconciler (#67), `RoadmapHealth` (#83) and governed apply
(#68) are required; temporal health (#85) is `required: false`.

`fixtures/roadmap-control-plane/live-capture.json` is an immutable live capture of that
graph. Replayed:

```bash
python roadmap/scripts/health.py roadmap/epics/roadmap-control-plane.yaml \
  --snapshot roadmap/fixtures/roadmap-control-plane/live-capture.json
```

it is `healthy`, with completion `incomplete` and `blocking: ["governed-apply"]` — #85 is
open but does not hold completion back. The first live run against #66 reported three
`DependencyMissing` entries: the dependencies existed only as prose in the issue bodies.
Native `blocked_by` relations were then created on GitHub, and the graph converged.

## Write boundary

Writes are deterministic and no LLM is in the apply path. The full pipeline:

```text
roadmap/epics/*.yaml  (merged, at git revision R)
          ↓
    schema + semantic + corpus validation
          ↓
    reconcile.py (GET only)      → RoadmapDiff
          ↓
    plan.py (pure)               → RoadmapApplyPlan
          ↓
    apply.py: per operation, re-read exactly its endpoints, then at most one
              allow-listed write through github_apply.py
          ↓
    RoadmapApplyResult + audit   → mctl-api evidence
          ↓
    reconcile.py again           → expect zero drift
```

`plan.py` is a pure function of the manifest bytes and the snapshot bytes: no clock,
no randomness, no absolute path, no network. Identical inputs produce byte-identical
JSON, which is what makes reviewing a plan the same decision as reviewing what will
be written.

### Diff entry to operation

One entry, one operation. Nothing else is actionable:

| `RoadmapDiff` entry | plan outcome | GitHub call at apply time |
| --- | --- | --- |
| `HierarchyMissingParent` | `AddSubIssue` | `POST /repos/{o}/{r}/issues/{parent}/sub_issues` |
| `HierarchyWrongParent` | `MoveSubIssue` | `DELETE .../issues/{observedParent}/sub_issue`, then `POST .../issues/{parent}/sub_issues` |
| `DependencyMissing` | `AddDependency` | `POST /repos/{o}/{r}/issues/{blocked}/dependencies/blocked_by` |
| `DependencyUnexpected` | `RemoveDependency` | `DELETE /repos/{o}/{r}/issues/{blocked}/dependencies/blocked_by/{id}` |
| `HierarchyUnexpectedChild` | note | none |
| `BindingUnbound` | note | none |
| `BindingIssueNotFound` | **refusal** | none |
| `BindingRedirected` | **refusal** | none |
| `BindingAmbiguous` | **refusal** | none |

Notes are recorded so the plan is a complete account of the diff, never actioned.
Creating issues for unbound work and removing children this manifest does not own are
both deliberately absent: the first would make the apply path author desired state,
the second would delete state the manifest never described.

### Refusal rules

Any binding-family drift refuses the **whole** plan for that manifest: zero
operations, not a partial apply. A redirect means somebody transferred the issue, a
not-found means it was deleted, an ambiguity means two authored bindings collapsed
onto one live object. Each is an externally changed binding, and the only correct
response is a human editing the manifest in a pull request.

Every operation endpoint must be an identity in `DesiredGraph.authored_keys()` — the
manifest's own bindings plus the issues it names in `externalDependsOn`. An operation
naming anything else raises `PlanRefused` rather than being emitted or quietly
dropped, so an unexpected relation to an outside issue stops the run instead of
reaching past what was reviewed. Targets are a subset of the authored identities by
construction, not by review.

The write client then holds a *narrower* boundary than the plan, because the two ends
of a write are two different claims. The issue a call is made **against** — the
repository in its path — must be in `DesiredGraph.owned_keys()`; only the issue named
in the call's body may come from the wider authored set. "We depend on their issue" is
something a manifest may say; "we may edit their issue's children" is not. The
distinction is load-bearing for `MoveSubIssue`, whose `observedParent` comes off the
observed graph rather than the manifest: an owned child observed under an
`externalDependsOn` issue plans a legitimate move back under the epic root, and the
remove half of that move would otherwise be a `DELETE` against a third party's
repository. It is refused at the mutator instead.

### Preconditions and idempotency

Each operation records the state it expects, in the vocabulary of `ObservedGraph`:

| operation | precondition | already satisfied when |
| --- | --- | --- |
| `AddSubIssue` | `ParentAbsent` — the child has no parent | the child already sits under the authored parent |
| `MoveSubIssue` | `ParentIs` — the child sits under the observed parent | the child already sits under the authored parent |
| `AddDependency` | `DependencyAbsent` | the blocked-by edge already exists |
| `RemoveDependency` | `DependencyPresent` | the edge is already gone |

Before every operation, `apply.py` re-reads exactly that operation's endpoints. No
write is ever issued without that re-read. Then:

- the end state already holds → `alreadySatisfied`, zero writes. Retry and replay
  converge to the same graph.
- the precondition holds → one allow-listed write → `applied`.
- anything else, including an endpoint that has since been deleted or transferred →
  `skipped` with reason `PreconditionChanged`, zero writes. A graph that changed
  after planning is never written over.
- the write returns a non-success status → `failed`, the run continues with the
  remaining independent operations and exits non-zero.

`opId = sha256(manifest sha256 + type + canonical endpoints)`, truncated to 16 hex
characters, and `planId = sha256(manifest sha256 + the ordered opIds)`. The same
operation therefore has the same id on every run, which makes `opId` a usable
idempotency key.

GitHub has no single call that reparents an issue, so `MoveSubIssue` is a remove
followed by an add. If the add half fails, the operation is `failed` with reason
`MoveIncompleteChildOrphaned` and the orphaned child named in its targets; the next
replay finishes the move, because its precondition is then "no parent".

### Allowed writes

`github_apply.py` is a separate module on purpose. `github_graph.py`'s safety
property is that it contains **no** mutation primitive at all, which is checkable by
reading one function; putting a writer beside it would replace that with a
convention. The write client is its mirror image — a closed allow-list:

```text
POST   /repos/{owner}/{repo}/issues/{number}/sub_issues
DELETE /repos/{owner}/{repo}/issues/{number}/sub_issue
POST   /repos/{owner}/{repo}/issues/{number}/dependencies/blocked_by
DELETE /repos/{owner}/{repo}/issues/{number}/dependencies/blocked_by/{dependency}
```

`MutationRefused` is raised **before transmission** for any (method, path template)
pair outside that set, for any `GET` (reads belong to `github_graph`), and for any
request touching an identity outside the owned set the caller supplied. HTTPS,
userinfo and origin rules are inherited from `github_graph.OriginBoundClient`, so the
credential rules of the read and write halves cannot drift apart. Redirects are the
one rule the two halves do *not* share: a read follows a same-origin `Location` (a
transferred issue answers with one), while a write refuses every `3xx` outright.
Following one would be worse than useless — urllib rewrites a redirected `POST` into
a `GET` and drops the body, so GitHub answers `200`, the operation is recorded
`applied` although nothing was mutated, and the module whose contract is that it
holds no read primitives has just issued a read. A redirect on a write means the
target moved, so the plan was computed against an identity that no longer holds and
has to be recomputed. GraphQL is unused here for the same reason it is unused in
the reader: an allow-list of four REST endpoints is checkable, an open-ended mutation
document is not.

`github_apply.FakeMutator` applies the same operations to an in-memory
`GitHubGraphSnapshot` under the same guard — the write-side analogue of
`FixtureGraphSource` — so the whole apply path is provable offline against the
existing fixtures, and the mutated snapshot still validates against
`github-graph-snapshot.schema.json`.

GitHub's sub-issue and dependency endpoints identify the *related* issue by numeric
id, which `github-graph-snapshot.schema.json` does not record. A live run therefore
supplies that mapping from outside, with `--issue-ids` (a JSON object of
`"owner/repo#number": id`); the write client will not read GitHub to find it, because
a reader inside the write module would defeat the point of the split.

### CLI

```bash
# what would be written, from a fixture; nothing is contacted
python roadmap/scripts/plan.py roadmap/epics/human-input.yaml \
  --corpus roadmap/epics \
  --snapshot roadmap/fixtures/human-input/converged-fixture.json

# re-read and report only: no --execute, so nothing is written
python roadmap/scripts/apply.py roadmap/epics/human-input.yaml \
  --corpus roadmap/epics --snapshot roadmap/fixtures/human-input/converged-fixture.json \
  --actor mctl-agents[bot]

# the real thing: merged bytes, a clean checkout, an approval bound to those bytes
python roadmap/scripts/apply.py roadmap/epics/human-input.yaml \
  --corpus roadmap/epics --live --execute \
  --actor mctl-agents[bot] \
  --approved-sha256 "$APPROVED" --proposal-id "$PROPOSAL" \
  --issue-ids issue-ids.json
```

`plan.py` takes `--corpus`, `--schema`, `--plan-schema`, `--snapshot`, `--live`,
`--capture`, `--api-base`, `--output`, exactly mirroring `reconcile.py`, and
`--snapshot` and `--live/--capture` stay mutually exclusive. Exit codes: `0` empty
plan, `1` a non-empty plan, `2` usage/IO/validation error, `3` refused.

`apply.py` takes the same corpus and source flags plus `--execute`, `--actor`
(required), `--approved-sha256`, `--proposal-id`, `--proposal-url`,
`--max-operations`, `--issue-ids`, `--capture` (offline: write the post-run snapshot
so it can be reconciled) and `--output`. Exit codes: `0` everything applied or
already satisfied, `1` something was skipped, `2` usage/IO/auth, `3` refused, `4` an
operation failed.

Guards, in order:

1. `--execute` is required for any write. Without it the run plans, re-reads and
   reports what would change.
2. The manifest's git revision is resolved with `git rev-parse HEAD`. A dirty
   checkout, or manifest bytes that differ from the bytes committed at that revision,
   refuses the run — the audit record has to be able to say "these bytes, at this
   revision".
3. `--approved-sha256`, when given, must equal the digest of those bytes, otherwise
   `ApprovalHashMismatch` and zero writes. Approval is bound to content, so a
   force-push after approval invalidates it automatically.
4. Every operation endpoint must be an authored identity of the manifest, and every
   identity a write is made *against* must be an owned one — `externalDependsOn` is
   authority to depend on a third party's issue, never to edit it. Both are asserted
   in phase 1; the authored assertion is re-run immediately before each write, and the
   owned-target rule is re-checked by the write client itself.
5. `--max-operations` (default 25) bounds the blast radius of the **run**, not of each
   manifest: every selected manifest is planned first and the operation counts are
   summed, so the whole-corpus invocation is capped at 25 writes in total. It refuses
   rather than truncating silently.
6. In live mode, `--issue-ids` must resolve an id for every operation before the first
   write. An incomplete map is a guard that fires at the start, not a `MutationRefused`
   that unwinds the run from operation five of nine — and an *absent* map (the flag
   omitted on a live `--execute` run that has operations to write) is the same guard
   failure at its widest, so it is refused up front too rather than aborting on the
   first operation. An offline run is not held to this: `FakeMutator` needs no ids.

Guards 1–6 are all evaluated for *every* selected manifest before the first mutation
is transmitted, so a guard firing on the last manifest of a whole-corpus run cannot
fire after the first one was already applied.

There is no `--plan` flag and no free-form target argument: the plan is always
recomputed from validated manifest bytes, so a hand-edited plan file is not an input
that exists.

### Audit and evidence

Every run emits a `RoadmapApplyResult`, including a run that stops early: if a write
lands and a later operation then fails or is refused, the accumulated result is still
serialized before the non-zero exit code is returned. An unrecorded write is the one
outcome this tool may not produce. Per operation the record carries `opId`, `type`,
`targets`, `outcome` in `applied | alreadySatisfied | skipped | failed` and a `reason` code for
the last two — plus one audit block:

```jsonc
"audit": {
  "actor": "mctl-agents[bot]",
  "proposal": {"id": "...", "url": "..."},   // or explicit null
  "manifest": {
    "path": "roadmap/epics/human-input.yaml",
    "sha256": "<64 hex>",
    "gitRevision": "<40 hex>"
  },
  "planId": "<64 hex>",
  "targets": [{"repository": "mctlhq/mctl-agents", "number": 333}],
  "manifestsSelected": 1                     // how many the run selected
}
```

`manifestsSelected` is what makes a truncated run legible from the artifact alone: a
whole-corpus run that applies the first manifest and then stops emits exactly one bare
`RoadmapApplyResult`, otherwise indistinguishable from a clean single-manifest run,
and the exit code that would have said otherwise is not archived beside the file.
Comparing it against the number of results present answers the question directly.

`roadmap-apply-result.schema.json` is `additionalProperties: false` throughout, types
every target as `issueRef`, and restricts `reason` to a closed vocabulary, so there is
no field an issue title, body or comment could be written into. A
`_forbidden_content_errors()` check runs before emission as an independent assertion
that none got there anyway, and the document is validated against its schema before
it is returned. `proposal` is an explicit `null` rather than an omitted key: absent
and unattributed must not look alike in an audit record.

Health is derived (see *Health* above) and must never author a second graph or a
competing desired-state file. The apply engine authors nothing either: it never
writes a manifest, never creates an issue, and never removes a child it does not own.

## RoadmapProposal integration

The authoring flow ends in one reviewable artifact rather than a sequence of generic
GitHub mutations:

```text
intent
  → RoadmapProposal            (mctl-api: persistence + authorization; stores manifestSha256
                                and an epicDefinitionPullRequest reference)
  → roadmap-decompose          (mctl-agents: the model step; emits an EpicDefinition draft only)
  → validate.py against the corpus   (schema + semantic + corpus, offline)
  → pull request to mctlhq/.github roadmap/epics/<name>.yaml
  → approval bound to the sha256 of the exact draft bytes
  → human merge
  → RoadmapApplyWorkflow       (mctl-agents: deterministic activity, no model invocation)
       plan.py + apply.py --execute --approved-sha256 <hash>
```

`roadmap-decompose` writes YAML and opens a pull request. It never touches a GitHub
graph endpoint, and its output is judged by `validate.py` rather than trusted.
`mctl-api` stays the `RoadmapProposal` persistence and authorization boundary and
holds the approved hash; `mctl-agents` stays the deterministic mutation boundary.

A model can influence the GitHub graph only by proposing *manifest text* that a human
merges — the same review gate that already guards `roadmap/epics/`. There is no path
from model output to a mutation target that does not pass through merged,
hash-pinned, validated bytes.

Apply is merge-triggered and manually re-runnable, never a cron: unattended
continuous writes would let a bad merge converge the whole graph before anyone read
the diff.

GitHub remains the planning UI. The manifest remains the canonical desired graph.

The Temporal workflow in `mctl-agents` and the `RoadmapProposal` field additions in
`mctl-api` are follow-ups against these published contracts; Projects v2 mutation and
issue creation for `BindingUnbound` work items are separately tracked, and both need
the read side to grow first.
