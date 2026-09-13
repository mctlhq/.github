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
  epics/
    human-input.yaml
  scripts/
    validate.py
  tests/
    test_validate.py
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

## Planned reconciliation boundary

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

Before writes are enabled, the drift detector must be mutation-tested in both
directions: it must stay green for a converged graph and turn red for deliberate
missing/incorrect relations. A detector that can only report drift is as broken as a
guard that can only pass.

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
