# First real backend demonstration (M4B)

Prepared for human review only. No candidate branch/commit/PR, remote settings,
Docker run, or paid model request is created by this implementation. The existing
automatic baseline workflow is unchanged. Local tests use inert providers and
Docker stubs; they cannot establish that a live demonstration succeeded.

## Proposed separate PR

Change **only** `backend/app/features/todos/validators.py`:

```diff
-    for field in model.model_fields_set - {"due_date"}:
+    for field in sorted(model.model_fields_set - {"due_date"}):
```

This makes the first explicit-null validation error deterministic, ordered by
field name. For example, `TodoUpdate(title=None, completed=None)` must report
`Field completed cannot be null.` Empty patches still fail; single non-null
values remain accepted; explicit null is still permitted only for `due_date`.
The current loop iterates an unordered set. The expected new test should cover
multiple invalid fields/ordering, rather than merely duplicate an existing
single-null case. Relevant source context includes `validators.py`, `schemas.py`
and the convention-matched `backend/tests/features/todos/test_validators.py`.
No database, network, clock, or external service is needed for this behavior.

The unapplied [patch](demo/deterministic-validation.patch) is supplied for review.
Application files in this checkout remain unchanged. **After** reviewing and
landing the trusted M4B implementation on `main`, a human should:

1. Record its full commit SHA as BASE. Freeze `main` for this demonstration:
   both the dispatch workflow SHA and the PR's current base SHA must equal BASE.
2. Create a separate candidate branch from that exact BASE. Apply the supplied
   patch, inspect the diff, and create one human-authored candidate commit. Do
   not add tests, manifests, agent code, workflows, or documentation to that PR.
3. Push that branch manually and open a same-repository PR targeting `main`.
   Record its PR number and full HEAD SHA. HEAD must differ from BASE. Check
   the PR contains exactly the one-line change above. Do not merge it yet.

No SHAs or PR numbers are invented in this repository. The host gate rejects
equal/malformed SHAs; GitHub metadata must match the current open same-repository
PR. Bounded pinned Git reads must then establish exactly the approved delta,
normal file mode, backend classification, and the existing matching test target.
Even a harmless additional comment, dependency change, extra test, mode change,
rename, or symlink is rejected. A dirty candidate working tree is ignored.

## Required GitHub administration before authorization

The new workflow is `.github/workflows/taskly-live-demo.yml`, named **Taskly
authorized AI backend demonstration**. Its only trigger is `workflow_dispatch`;
the default paid authorization input is false. It accepts SHA/PR identities,
never arbitrary commands, test paths, models, or spending settings.

Before a paid dispatch, a repository administrator must configure the
**taskly-live-demo** environment:

- Permit deployments from protected `main` only. Enable required reviewers,
  prevent self-review, and disallow protection bypass where available. Reviewers
  must inspect the exact workflow SHA, BASE, HEAD, PR diff and fixed budget.
- Set the environment variable `TASKLY_DEMO_BASE_SHA` to the approved full BASE.
- Store `OPENAI_API_KEY` as an **environment-only secret**. Do not create a
  repository/organization secret with that name that arbitrary branch workflows
  could access. Review all workflows allowed to use this environment.
- Protect `main` and changes to the workflow/agent/provisioning inputs. Do not
  allow an unreviewed workflow definition to access the environment secret.
- Confirm the configured model is available to the project and review the
  existing transport price table before approving spending. The fixed config
  uses `demo_config(budget_usd=0.10)`: review disabled, at most two generation
  calls, one repair, existing input/output limits and USD 0.05 call reservations.
  Set appropriate provider-side controls independently; the software guard is
  not an account-level hard billing limit.

Environment protections are external repository settings, not established or
verified by a YAML file. A missing approved-base variable or secret fails closed.
A missing environment alone may be auto-created by GitHub; it must not be
mistaken for an environment with enforced reviewers. If main-only restrictions,
required review, or suitable secret scoping cannot be enforced, **do not run this
workflow with a key**. The smallest alternative is the existing
[END_TO_END.md](END_TO_END.md) procedure on a trusted disposable Linux machine
with a reviewed installation and a separately authorized single run. Do not
relax Docker restrictions or switch to a privileged PR trigger.

Official GitHub references: [environment protections and secrets](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments),
[manual dispatch semantics](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch),
[pinned artifact action](https://github.com/actions/upload-artifact/commit/ea165f8d65b6e75b540449e92b4886f43607fa02).

## First explicitly authorized real run

1. Complete the human-created PR and environment setup above. Ensure `main`
   still equals BASE and the PR's current head equals HEAD. Review the exact
   source, nearby schemas, and existing tests for secrets before permitting
   provider disclosure. Confirm the baseline workflow passes on this PR.
2. Open Actions → **Taskly authorized AI backend demonstration** → Run workflow.
   Select **main**; enter the exact BASE, HEAD, and PR number. Check `allow_paid`
   only after approval for **one run, at most two API calls, USD 0.10 estimated
   cost**. Dispatch once. This action itself was not performed during development.
3. Have an independent environment reviewer confirm the same inputs/workflow
   SHA and approve the environment job. No key is exported to the host process
   until the paid step. The read-only GitHub token is used only for checkout and
   the fixed PR metadata endpoint; it is not in the paid process environment.
4. The job provisions the reviewed agent from BASE, validates actual GitHub PR
   metadata, admits the exact Git diff, creates the fixed configuration, and
   builds the existing Dockerfile from BASE only with
   `TRUSTED_REVISION=BASE` and `docker build --iidfile`. It then invokes the
   existing `taskly_agent.end_to_end` CLI once with the immutable local image
   ID and all pinned parameters. No candidate code is installed/imported on the
   host. The candidate checkout supplies Git data, never shell/Python scripts.
5. Before opening its provider session, the CLI requires the existing Linux
   Docker smoke probe. Generation uses the real Responses transport; proposal
   scope/identity checks remain in the adapters and applier. Each proposal is
   applied to a disposable pinned workspace. Only real Docker invocation receipts
   can produce available evidence. One repair is possible after available failed
   verification; missing/unavailable evidence or provider errors do not retry.
   Containers receive fixed commands/env, no key/GitHub credentials, no network,
   no host mounts/socket, and the existing resource/security restrictions.
6. Download **taskly-live-demo-RUN_ID** from that workflow run. It contains only
   `evidence.json`, retained for three days. Check `trusted_revision`, immutable
   `image`, configured `model`, and `report.identity` against approved values.
   For success require backend classification, overall passed, available/passed
   independent evidence, matching head/policy/proposal identities, generated
   path `backend/tests/features/todos/test_validators.py`, real test counts and
   coverage, known nonzero input/output tokens, at most two model calls, at most
   one repair, and accounted estimated cost no greater than USD 0.10. A skipped,
   blocked, incomplete, failed, malformed or missing report is not success.
7. Review the sanitized job summary and cost. Never rerun automatically after
   failure/cancellation: rerun attempts are disabled. A new dispatch needs new
   human/environment approval and its own spending allowance; previous unknown
   spending must be considered manually. Disable/remove the environment secret
   after the demonstration. No generated test is committed, pushed or published.

The CLI's raw stdout/stderr files remain private host-temporary files and are
not echoed or uploaded. A separate admission/publication helper revalidates the
typed report, identity, expected generated path, evidence policy and numeric
limits, and suppresses arbitrary diagnostics. Only its fixed summary and
projected JSON artifact are published. Pipeline failure keeps the job failed
even if safe failure evidence can be uploaded. A malformed artifact is never
uploaded. A model refusal, missing dependency, unavailable model/runtime or
budget exhaustion must be reported as a failure/unavailable result, not bypassed.

## What this can and cannot demonstrate

The first real run is still pending. Deterministic integration tests prove
wiring and rejection behavior, not real model quality, real Docker availability,
or real test results. A successful hosted run can establish paid generation,
safe application and independent sandbox execution correlated to the proposal.
Aggregate passing counts alone do not establish assertion quality; generation
quality must be assessed separately before claiming the new tests meaningfully
cover the changed behavior. Raw generated test contents are intentionally not
published by this demonstration. The report binds their proposal digest.

No coverage increase is claimed: this path does not run a comparable unmodified
baseline. No production publishing or cross-run durable budget ledger is added.
The cumulative ledger is process-local; a new job/process gets a new allowance.
External/provider price changes, upstream package/image supply chain, trusted
runner/admin configuration, Docker's shared kernel, and adversarial test-report
forgery remain the limitations documented in [CI_EXECUTION.md](CI_EXECUTION.md)
and [OPENAI_TRANSPORT.md](OPENAI_TRANSPORT.md). Artifacts are observations from a
trusted workflow, not cryptographic attestations or privileged deployment input.
