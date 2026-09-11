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

## Mergeability is a separate question from review

`mergeStateStatus: BLOCKED` on its own is not actionable, and an approved,
green, unmergeable PR is exactly the shape that reads as ready to a person
skimming. `merge:` reports why:

| value | meaning |
|---|---|
| `ready` | mergeable now |
| `blocked-review` | a reviewer is asking for changes |
| `blocked-review-required` | nobody has reviewed it yet, and the base wants an approval |
| `blocked-conversations:N` | approved, but N review threads are unresolved |
| `blocked-behind-base` | the branch is behind a base that requires strictness |
| `blocked-checks` | a check is failing or pending |
| `conflicted` | conflicts with the base |
| `blocked-unresolved-check` | blocked, and we cannot say why |

`blocked-review` and `blocked-review-required` are kept apart because they
call for opposite actions: one wants the author to work, the other wants a
reviewer to look. Every pre-review PR under branch protection sits in the
second, so folding them together would misroute the most common blocked state
in the org.

An unresolved thread counts whether or not GitHub marks it outdated: a thread
going outdated because its line was edited does not resolve it, and the merge
box keeps refusing. Excluding them drove the count to zero on precisely the
PRs this vocabulary exists to explain.

Review outranks threads when both are true: a reviewer asking for changes is
the cause, open threads the symptom. `blocked-unresolved-check` is deliberate —
a block we cannot explain is worth a person and must not be filed under one we
can. The count in `blocked-conversations:N` is evidence, not identity: a
declaration of `blocked-conversations` matches whatever N happens to be, so the
report does not churn as threads are resolved one at a time.

## One assertion, several acceptable answers

`review:` and `merge:` accept a list. Some assertions genuinely have more than
one right answer at different moments of the same situation: a PR under active
review alternates between `blocking-findings` and `unreviewed-head` every time
its author pushes a fix. Declaring a single value there would report drift on
every push, and a report that cries wolf on normal work is worse than no
report.

Every value is checked against the vocabulary the tool can actually produce, so
a typo is a hard error rather than an assertion that never matches.

## Liveness, and the limit of it

The snapshot is committed only when the reconciled state changes, because it
carries a timestamp and would otherwise take four commits a day saying nothing.
That leaves the question the rest of this file exists to ask: how do you know
the reconciler ran at all?

Not from the snapshot. Its `generated_at` records the last *change*, and "no
change" is the designed steady state — measuring staleness against it would
report a growing outage forever on a roadmap that is simply quiet. The
reference is the workflow's own run history, which advances on every run, and
`--previous-run-at` carries it in.

Be exact about what that buys. It reports an outage that has **ended**, on the
first run after it. A reconciler that is still down produces no run and
therefore no report; catching that needs a watchdog outside this workflow, and
there isn't one. This is a known gap, not a solved problem.

Three different things can be wrong and they are not the same message, so the
report distinguishes them: the roadmap diverged, items could not be observed,
or the reconciler was not running.

The report is a **tracking issue**, not a failed job and not an annotation. An
annotation on a scheduled run reaches nobody. A failed job would have been
worse than useless here: two of the three conditions are standing states by
design, so the job would never succeed again — and the liveness reference above
is the last *successful* run, which would then recede without bound until the
tool started reporting an outage that was not happening. The job's conclusion
keeps meaning "the reconciler worked".

The issue's body is rewritten every run and carries the current state; a
comment — the part that notifies — is added only when the reconciled state
changes; and the issue is closed when everything is aligned again. Blindness
keeps the issue open for as long as it lasts, because a partially visible org
must not be able to go quiet behind a half-red page.

Its identity is a **label**, not its title. The issue stays open while the
condition lasts, so it sinks out of any "newest N" window while working
correctly — a title match would then file a fresh one every six hours. The
label also survives an edited title and a manual close, which frees the title
to name whichever condition matters most, so an ended outage cannot file an
issue saying the roadmap is not aligned above a body saying it is.

One honesty note on the wording: the liveness gap is measured against the last
**successful** run, so it reports "did not complete successfully", not "did not
fire". An exit-2 run, a push that lost three races, or a failure while
reporting all widen it without the schedule having missed a tick.

A run that could observe nothing at all exits 2 and fails the job rather than
publishing an all-red page: every item unobserved is a credentials or
connectivity fault, not a roadmap state.

## The declaration is validated, not trusted

An unrecognised key under `expected:` is the worst defect this file can carry:
it reads like an assertion, renders like one, and checks nothing, so the item
reports OK forever on a line nobody evaluates — a passing test that never ran.
`validate_state` rejects the whole run (exit 2) on an unknown key, an item that
asserts nothing, a duplicate id, an unparsable expectation or a bad regex.

The same instinct applies to paged GitHub data. Truncation is detected with
`pageInfo` — `hasNextPage` for `first:` connections, `hasPreviousPage` for
`last:` ones — and a connection with more beyond the page raises rather than
being counted, because a count over a truncated list is a guess.

Not `totalCount`: on a filtered connection the two disagree.
`IssueTimelineItemsConnection` carries three separate counters precisely
because `totalCount` does not describe what an `itemTypes:` filter returned,
and `PullRequestReviewConnection` has none that accounts for `states:`.
Comparing an unfiltered total against a filtered list made every issue past a
hundred timeline events permanently `OBSERVATION_FAILED` — the guard producing
the failure it exists to prevent. Anyone adding a connection should follow
`nodes_of`, not that earlier instinct.

Probes cover assertions GitHub issue state cannot make — a file's contents, a
count of services that opted into something. A probe that cannot read what it
needs raises rather than returning zero, which is the whole point.
