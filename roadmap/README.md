# Roadmap state reconciler

`roadmap-state.yaml` declares what we believe is true about the roadmap.
`reconcile.py` observes GitHub and reports where belief and reality disagree.
`ROADMAP.md` and `snapshot.json` are its output — generated, never edited.

    roadmap-state.yaml ──> reconcile() ──> snapshot.json ──> ROADMAP.md
        (desired)            (observe)       (machine)         (page)

## Why not a watcher

A watcher reports events. It cannot report the absence of one, because from the
outside "nothing happened" and "I failed to look" are the same empty result.
On 2026-09-11 a watcher in this org stayed silent for six hours while six pull
requests opened: its `gh` call was failing and `|| true` swallowed the error.
Nobody could tell, because a broken watcher and a quiet org produce identical
output.

So this tool asserts state rather than following events, and an observation it
could not make is a red result:

| state | meaning |
|---|---|
| `ALIGNED` | every assertion was observed and matches |
| `DRIFT` | observed, and it disagrees with what we declared |
| `UNEXPECTED_SILENCE` | state agrees, but something believed active has not moved within `max_silence` |
| `OBSERVATION_FAILED` | an assertion could not be checked — never folded into "nothing found" |

`OBSERVATION_FAILED` outranks `DRIFT`: a divergence you could not observe is not
a divergence you may report.

## Reacting to a report

`DRIFT` has exactly two causes, and both want a person:

* reality moved and the declaration is stale — edit `roadmap-state.yaml`;
* reality moved and it should not have — fix reality.

A goal not yet met is declared as the goal, so it reports `DRIFT` until it is
met. That is deliberate, and it is not noise: the workflow notifies only when
the reconciled state *changes*, so a standing gap is announced once and then
sits on the page until it closes.

## Running it

    python3 roadmap/reconcile.py --selftest          # fixtures, no network
    python3 roadmap/reconcile.py \
        --state roadmap/roadmap-state.yaml \
        --json roadmap/snapshot.json \
        --markdown roadmap/ROADMAP.md

Exit codes: `0` aligned, `1` something is not aligned (a normal result to
publish), `2` the run itself could not be completed.

Probes cover assertions GitHub issue state cannot make — a file's contents, a
count of services that opted into something. A probe that cannot read what it
needs raises rather than returning zero, which is the whole point.
