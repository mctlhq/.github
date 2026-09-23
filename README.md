# roadmap-state — generated, do not edit

This branch is **generated state**, written only by the
[`Roadmap publication`](https://github.com/mctlhq/.github/blob/main/.github/workflows/roadmap-publish.yml)
workflow (mctlhq/.github#119). It is not a planning source: the manifests in
`roadmap/epics/` on `main` and the issues on GitHub are. A hand edit here is
overwritten by the next run and, until then, fails `publish.py verify`.

| File | Kind | What it is |
|---|---|---|
| `publication.json` | `RoadmapPublication` | Provenance: evaluator and source revisions, manifest digests with each epic's name, lifecycle, title and goal, the observation, and a digest of each file below |
| `snapshot.json` | `GitHubGraphSnapshot` | The one GET-only observation everything below is derived from |
| `ready-set.json` | `RoadmapReadySetList` | `ready.py` over every manifest, replayed from `snapshot.json` |
| `health.json` | `RoadmapHealthList` | `health.py` over every manifest, replayed from `snapshot.json` |

## Reading it

- **Freshness is `publication.json` → `observation.capturedAt`**, the time of the
  capture, not the time of the commit. A run that cannot observe, evaluate or
  verify pushes nothing, so a failed refresh leaves the older `capturedAt` in
  place; consumers decide how old is too old.
- A `synthetic-fixture` observation carries no `capturedAt` and is never fresh.
- Check each file's sha256 against `publication.json` → `files` before trusting it.
- `unknown` is not `blocked`: an item with `completion.reason: unbound` has no
  bound issue, and any other `unknown` is state that could not be proven.
- Anyone can reproduce a publication from its own snapshot:

```bash
git checkout <source.revision>
python3 roadmap/scripts/publish.py verify <directory holding this branch>
```
