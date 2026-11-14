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

## Automatic dispatch (mctlhq/.github#347)

`agy-formal-auto-dispatch.yml` starts `agy-formal-dispatch.yml` for a target PR
only when the verdict is likely to stay current. Its decision lives in
`.github/scripts/agy_formal_auto_dispatch.py` on this repository's default
branch and reads only API metadata, never PR-head files or PR text. For the
current head of an open, non-draft, same-repository PR it requires:

1. every other required check of the base branch's rules passing (GitHub's own
   rule: `success`, `neutral` or `skipped`), each from its bound integration;
2. a `claude[bot]` `APPROVED`/`CHANGES_REQUESTED` review with that `commit_id`
   whose body records `Reviewed head=<head>`; a verdict from an older pinned
   caller without that line counts only if it was submitted no earlier than the
   head commit was created, which an approval GitHub moved onto an
   update-branch head never is;
3. `behind_by == 0` against the base branch tip;
4. no `agy-formal-review` check on the head, no active dispatch run for the PR,
   and fewer than two formal runs active (two runners serve every agy review).

Active dispatch runs are found by their `run-name`, which records repository,
PR and head (`any` for a manual run without `head_sha`). A run pinned to an
older head of the PR is cancelled unless its publish job has started. An active
run whose title does not parse, or a manual run for the PR, blocks dispatch and
is never cancelled. The dispatched run carries `head_sha`; prepare starts
nothing if the PR has moved on, so the required check stays pending for the new
head instead of reviewing a head nobody checked.

Any failed, partial (later page, short of `total_count`) or malformed read
dispatches nothing and the next round retries. The poller never creates or
completes `agy-formal-review`.

Trigger: a five-minute schedule. `workflow_run` and `check_suite` reach only
workflows of the repository the checks ran in, so mctl-gitops events cannot
start a workflow here, and a relaying caller there would move the decision, or
a token able to dispatch here, next to PR-controlled code.

Activation: the poller runs dry (decisions in the job summary only) until the
repository variable `AGY_FORMAL_AUTO_DISPATCH` is `true` in `mctlhq/.github`.
Manual `agy-formal-dispatch.yml` stays the override. Do not also activate a
`pull_request_target` caller for the same repository (mctl-gitops#2605 as
drafted): it would start a formal run on every push, which is the waste this
removes, and two dispatch sources refuse each other's checks.

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
