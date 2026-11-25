# Agy formal GitHub reviews

Status: implementation prepared; NOT LIVE. No reviewer App credentials or branch
protection changes have been provisioned by this change.

## Goal and trust boundary

A completed Agy review of the current PR head publishes a real GitHub review:
`PASS` becomes `APPROVE`; `FAIL:P1`, `FAIL:P2`, and `FAIL:P1,P2` become
`REQUEST_CHANGES`. Existing informational callers keep their behavior. Neither a
previous PASS comment nor a green workflow without a usable model verdict counts
as approval. This technical review does not authorize a human-only release grant,
a proposal acceptance, or a production activation.

The opt-in wrapper is `.github/workflows/agy-formal-review.yml`. It accepts only a
default-branch `pull_request_target` or `workflow_dispatch` run, same-repository
non-draft PRs targeting the default branch, and same-owner repositories. The
publisher never checks out PR code: it resolves the reusable workflow's actual
commit from GitHub's `referenced_workflows` and checks out only the publisher
scripts at that commit. An unreadable/ambiguous source refuses publication.

The model runs on the existing self-hosted reviewer without the reviewer App
key. Its structured result travels through the trusted workflow job graph, not a
PR-uploaded artifact. Formal mode ignores caller conventions and rejects empty,
truncated, or opaque binary diffs. The App key is used only in separate ephemeral
GitHub-hosted prepare/publish jobs. Privileged jobs never run model output as code.
GitHub's ordinary `GITHUB_TOKEN` approval setting stays disabled.

The publisher binds repository, PR, head, base, source repository, run, attempt,
model, and verdict. It re-reads policy and current head/base before publication,
uses an explicit review `commit_id`, and rechecks after posting. A race that moves
head/base triggers dismissal of the just-posted approval. A separate App-bound
`agy-formal-review` check prevents another workflow from supplying a successful
status under the same name. Strict required checks and dismissal on push are
mandatory. Missing data, API failures, or failed later pagination pages fail
closed. No bypass permission is requested.

## Owner/operator activation

1. Merge the shared-code PR after its current-head reviews and CI pass. Record
   the merged 40-character SHA below. Do not activate a PR-head workflow with
   publisher credentials.
2. An organization owner creates a dedicated private GitHub App, suggested name
   `mctl-agy-reviewer`. Homepage: `https://github.com/mctlhq/.github`. Disable
   webhooks and OAuth-on-install. Repository permissions only:
   - Contents: read (PR metadata/checkout access).
   - Administration: read (effective branch protection verification).
   - Checks: write (the independently bound check).
   - Pull requests: write (reviews and stale-review dismissal).
   - Metadata: implicit read. No organization permissions or write access to
     contents, workflows, environments, or administration.
3. Install the App on **selected repository `mctl-gitops` only** for the pilot.
   Do not reuse a more privileged implementer/release App or give it bypass roles.
4. In `mctlhq/.github`, create environment `agy-review-publisher`, restrict it to
   default branch `main`, and store environment variable `AGY_REVIEW_APP_ID` plus
   environment secret `AGY_REVIEW_APP_PRIVATE_KEY`. The owner provisions the
   private key directly; never paste it into chat, logs, files committed to git,
   or an issue. Do not relax branch restrictions for a test branch. A required
   environment reviewer is optional operator policy; both privileged jobs will
   need that approval if enabled.
5. In the pilot's existing main protection, preserve all current rules and bypass
   actors. Keep stale-review dismissal enabled, enable strict required status
   checks, and add required check context `agy-formal-review` with `integration_id`
   equal to the new App ID. Do not accept a check from any integration. Inspect
   both rulesets and classic protection; this is a deliberate merge gate and
   will hold PRs until a real review finishes.
6. Bootstrap using `.github`'s **default-branch** manual workflow
   `agy-formal-dispatch.yml`, inputs `repository=mctlhq/mctl-gitops`,
   `pr_number=2604`. It obtains its own fresh Agy result; no existing PASS is
   promoted. The dispatching actor must have workflow write access.
7. Verify the acceptance checklist below before merging that target PR. The
   App review does not satisfy an independent human-only approval requirement.

Pilot caller draft: [mctl-gitops#2605](https://github.com/mctlhq/mctl-gitops/pull/2605).
It remains inactive until bootstrap acceptance. After bootstrap, review and
activate the pilot caller replacing the old informational caller. Pin the reviewed shared wrapper to its merged SHA:

```yaml
name: Agy formal review
on:
  pull_request_target:
    types: [opened, reopened, synchronize, ready_for_review]
  workflow_dispatch:
    inputs:
      pr_number:
        type: number
        required: true
permissions: {}
jobs:
  formal:
    uses: mctlhq/.github/.github/workflows/agy-formal-review.yml@<MERGED_SHA>
    permissions:
      contents: read
      actions: read
      pull-requests: write
    secrets:
      AGY_REVIEW_APP_PRIVATE_KEY: ${{ secrets.AGY_REVIEW_APP_PRIVATE_KEY }}
    with:
      pr_number: ${{ fromJSON(format('{0}', inputs.pr_number || github.event.pull_request.number)) }}
```

Create the same protected environment/variable/secret in the pilot repository
before enabling that default-branch caller. The caller must pass the named
secret explicitly, even when it only exists in the environment. GitHub resolves its value from the protected environment in
the privileged jobs. Do not use `secrets: inherit`.
Do not run both dispatch sources concurrently for the same head. Check ownership
is intentionally conservative: once the central bootstrap owns a head, switching
source repository on that same head refuses; use the central dispatcher until
that PR merges, or a fresh PR head for the new caller. Within one source, newer
runs/attempts can supersede older checks; old publishers cannot overwrite them.
A required-check failure after quota exhaustion requires a real rerun, never a
fabricated result or a manual success status.

## Acceptance and evidence

Local unit tests exercise the actual publisher and actual inline result exporter
with synthetic API responses. They cover genuine PASS/FAIL publication requests,
idempotency, stale head/base, a push during publication, exact run/attempt,
current policy, failed reads and later pages, missing output, malformed results,
identity, protected code checkout, and compatibility defaults. Mutation tests
remove four critical refusals and confirm the corresponding tests turn red,
then restore the original implementation and confirm green. These tests do not
prove live App permissions or GitHub review counting.

The actual prompt preparation shell also runs against temporary Git fixtures:
complete text passes; empty, oversized, and binary diffs refuse. Disabling the
real guard makes those rejected fixtures pass; restoring it restores refusal.
The actual CLI entrypoint is tested with missing cleanup metadata and a newer
check owner: it preserves the original publication exception, reports unknown
cleanup state, and leaves the newer check untouched.

Live acceptance (not yet run):

- A fresh Agy PASS produces one App-authored `APPROVED` review with `commit_id`
  equal to the current PR head; GitHub's review requirement counts it.
- The required check is supplied by that App ID on that SHA. A normal Actions
  check/comment with the same text cannot satisfy it.
- A deliberate P2 fixture produces `CHANGES_REQUESTED` and a failed check.
- Pushing after review invalidates approval and requires a fresh result.
- Missing verdict/quota/model timeout produces no approval. An API write failure
  can have an unknown outcome: the check must remain non-successful, and the
  operator inspects actual reviews before retrying. Never report an unobserved
  write as absent.
- Base movement prevents stale publication; strict checks require an up-to-date
  branch. Untrusted events, fork PRs, draft PRs, or a PR-editable conventions
  string cannot obtain approval.
- Old informational callers still comment and cannot mint an App token.

Activation record: shared merged SHA, App ID/slug, selected installation,
protection diff, dispatch run URL, PR review URL and exact head SHA are pending.

Rollback: disable the formal caller/dispatcher, revoke the dedicated App key or
uninstall it, then remove only the newly added App-specific required check after
owner review of the fallback gate. Keep existing protections and bypass actors.
Reinstate the informational caller if desired; it is not an approval substitute.

## Merge queue commit: carrying the PR-head verdict

The queue commit is reviewed by the model again only when that adds information.
`agy_review_publish.py` (prepare mode, queue entries only) marks the queue commit
`agy-formal-review` = success without calling the model when ALL of these hold,
each re-read from the API by the trusted publisher:

- the queue ref, its commit and its parents are exactly `[base, live PR head]`
  (the existing `queue_state` proof; MERGE method, group of one);
- the live PR head carries exactly one App-owned `agy-formal-review` that is
  `completed`/`success`, bound to that head SHA, from a formal run for this
  repository and PR, and is not itself a carried mark;
- `base...PR head` and `base...queue commit` list the same non-empty set of
  (path, status, previous path, blob SHA), the queue comparison's merge base is
  the base, no entry is mode-only or a pure rename, and fewer than 300 files;
- the queue ref still points at the commit after those comparisons.

Any other outcome (missing, neutral, failed or stale head verdict, a different
blob or an extra path, an unreadable or malformed API body, a failed later page)
logs `queue verdict not carried` and runs the model exactly as before. A carried
check has the title `Agy formal review: success (carried from PR head)` and never
licenses a further carry. Inputs from the caller stay untrusted and unchanged.

## Independent review gate controller (mctlhq/.github#398)

To use `mctl-agy-reviewer` strictly as a fallback when Claude is unavailable on
ordinary PRs (without running Antigravity on every PR or duplicating review
work), `.github/workflows/review-gate-controller.yml` runs every 5 minutes (and
on `workflow_dispatch`) from `mctlhq/.github` `main` using environment
`agy-review-publisher`:

- **Fail-closed Claude outcome classification (`claude-review.yml`, fixes #287):**
  `claude-review.yml` verifies execution JSON output on every attempt, requires an
  unedited `claude[bot]` terminal review (`APPROVED` with `Reviewed head=... base=...`
  or `CHANGES_REQUESTED`), exits `1` on quota/rate-limit errors
  (`NO_VERDICT_TRANSIENT`), and emits `review_outcome` (`PASS`, `BLOCKING_FINDINGS`,
  `NO_VERDICT_TRANSIENT`, `NO_VERDICT_OTHER`, `STALE`, `SELF_EDIT`).
- **Central gate check (`independent-review` / `independent-review-shadow`):**
  `review_gate_controller.py` evaluates open non-draft same-repo PRs against their
  default branch. It enforces blocking `CHANGES_REQUESTED` precedence, checks
  trusted unedited `claude[bot]` `PASS` via GraphQL + caller workflow provenance,
  and marks `independent-review` (or `independent-review-shadow` in shadow mode)
  green without invoking Antigravity.
- **Fallback dispatch & reuse:** Only on trusted `NO_VERDICT_TRANSIENT` (or
  allowlisted `timed_out`/`startup_failure` with an unmodified caller workflow) or
  trusted `SELF_EDIT` does the controller dispatch `agy-formal-dispatch.yml` for
  the exact `head_sha` under `agy-review/v2` + canonical `pr_diff_digest_v1`.
  Qualifying exact-head/digest Agy verdicts are reused across ticks, and terminal
  fallback failures suppress infinite `*/5` re-dispatch until `head_sha`/`diff_digest`
  moves or `force_rerun=true` is dispatched.
- **Shadow-first activation & kill switch:** Until repository variable
  `AGY_REVIEW_GATE_ENABLED=true` is set, the scheduled controller stays in dry-run
  mode and `AGY_REVIEW_GATE_REPOS` defaults to `mctlhq/mctl-gitops:shadow`. In
  merge queues, `agy_review_publish.py` `carry_proof` also carries trusted unedited
  `claude[bot]` `PASS` verdicts (as well as active unedited `mctl-agy-reviewer[bot]`
  approvals) without re-running Antigravity. This Phase 1 architecture supersedes
  the always-on per-PR draft (#348).
