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
  epics/
    human-input.yaml
  fixtures/
    human-input/
      converged-fixture.json
  scripts/
    validate.py
    github_graph.py
    reconcile.py
  tests/
    test_validate.py
    test_reconcile.py
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

Only a 404 or 410 means an issue does not exist, and only a 404 or 410 on `/parent`
means it has no parent. Any other response that cannot be read as the expected shape
is an observation error (exit 2), never an empty relation list: malformed provider data
must not become an observed absence.

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
relative to the repository root, and the snapshot's own provenance. It contains no
timestamp and no random value of its own, so identical manifest and snapshot bytes
produce byte-identical JSON on any machine, in any input order.

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

## Planned write boundary

The target runtime split is:

```text
.github/roadmap/epics/*.yaml
          ↓
    schema + semantic validation
          ↓
 RoadmapReconcileWorkflow (mctl-agents)
          ↓
 live GitHub graph read
          ↓
 deterministic RoadmapDiff
          ↓
 mctl-api observed/read model
```

Write reconciliation comes later and must be deterministic: no LLM is allowed in the
apply path. The exact manifest revision/hash must be carried into audit/evidence.

Operational health -- `ALIGNED`, `DRIFT`, `UNEXPECTED_SILENCE`, `OBSERVATION_FAILED` --
is a later derived layer over `RoadmapDiff` plus observed state. It must not author a
second graph or a competing desired-state file.

## RoadmapProposal integration

The eventual authoring flow should produce a reviewable manifest rather than a
sequence of generic GitHub mutations:

```text
intent
  → RoadmapProposal
  → roadmap-decompose
  → EpicDefinition draft
  → exact-hash approval
  → PR in this directory
  → deterministic reconciliation
```

GitHub remains the planning UI. The manifest becomes the canonical desired graph.
