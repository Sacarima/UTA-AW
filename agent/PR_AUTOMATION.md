# Automatic backend/frontend PR agent (M5)

This implementation is prepared for review, not deployed. No workflow, secret,
PR comment, paid request, commit or push was created during development.
The existing secret-free `taskly-verification.yml` and successful M4C manual
`taskly-live-demo.yml` are unchanged. Backend-only and frontend-only generation
share the existing pipeline. Mixed generation, coverage backfill, queues,
databases, schedulers and publishing of commits remain outside this milestone.

## Events and trust boundaries

The new `taskly-pr-agent.yml` follows **Taskly isolated verification** using
`workflow_run: completed`. The existing baseline starts for PR `opened`,
`synchronize`, `reopened`, and `ready_for_review` events. After each baseline
completion, the follower resolves the current PR through authenticated GitHub
metadata, checks the source workflow name/path/event/repository, and analyzes
the real pinned base/head Git objects. A failed baseline still permits analysis
and reporting, but prevents paid generation. This follower must first be landed
on the default branch; candidate workflow/agent changes never supply its code.

GitHub explicitly warns that `workflow_run` can access secrets/write tokens.
We therefore never consume source-workflow artifacts, caches, scripts or test
reports, and never install or import candidate code on the host. Trusted agent
code is checked out at the follower's default-branch SHA. Candidate checkout is
used as Git data only. Only the existing DockerSandbox executes candidate code.

Three jobs separate credentials:

1. **analyze** is always secret-free and read-only. It uses GitChangeAnalyzer
   and trusted bundled skills; its artifact contains sanitized typed analysis,
   pinned identities and explicit unavailable execution fields. Forks and
   untrusted authors can receive analysis; they cannot authorize model calls.
2. **generate** uses a different `taskly-pr-agent` environment and read-only
   GitHub token. It rechecks PR freshness and authorization before building and
   immediately before invoking the existing end-to-end orchestration. Only its
   admission/generation Python steps receive OPENAI_API_KEY. The image build
   does not. The existing provider, context collector, generator, controller,
   applier and independent verifier perform the pipeline without replacements.
3. **publish** has PR-comment write permission but no model credential, candidate
   checkout, model calls or Docker execution. It downloads JSON only from this
   trusted follower's jobs, parses strict contracts as data, checks the current
   PR base/head again, and posts a sanitized commit-bound comment. It never
   downloads/applies/executes the generated patch. No PR-controlled program runs
   with either a model key or write token on the host.

The Docker subprocess still receives its existing clean environment and fixed
stdin job/archive, never host credentials. Containers retain network=none,
non-root, no host mounts, read-only root, capability/resource restrictions and
validated one-use invocation receipts. There is no host-test fallback.

## Exactly when real AI generation occurs

By default **all PRs are analysis-only**. A repository administrator must first
review/land this code and explicitly enable the following configuration:

- Repository Actions variable `TASKLY_PR_AI_ENABLED=true`.
- A separate environment **taskly-pr-agent**, restricted to protected `main`.
- Environment variable `TASKLY_PR_AGENT_BASE_SHA` set independently to the
  reviewed default-branch commit containing this implementation.
- Environment-only secret `OPENAI_API_KEY` in **taskly-pr-agent**. The existing
  M4C environment/key are not reused or altered. Do not put the key in repository
  secrets or add it to the baseline/publisher job.

Keep approval binding step-scoped using `vars`; configuration variables are not
implicitly exported as shell variables. The independently approved SHA must
equal both the PR's base SHA and the follower's actual default-branch SHA. A
default-branch update intentionally pauses generation until independently
reviewed/reapproved; never substitute the PR input for the approval variable.

A PR is eligible for the real API only when **all** these checks pass:

- It is an open, non-draft, same-repository PR targeting `main`.
- GitHub author association is OWNER, MEMBER or COLLABORATOR. A fork or other
  association is analysis-only even if a key happens to be configured.
- Git metadata identifies surviving backend-only or frontend-only production
  targets and an add/update decision. Mixed, tests-only and documentation-only
  changes do not invoke the model. Frontend generation selects the bundled
  `react-unit-tests` guidance, plus `update-existing-tests` when appropriate.
  Only analysis-authorized `.test.ts`/`.test.tsx` files under `frontend/src/`
  can be created/modified; fixtures, setup, configuration and production files
  remain protected. Independent frontend verification uses the existing fixed
  Vitest command and JSON test/coverage reports inside Docker.
- The matching baseline completed successfully on its first attempt; source
  base/head metadata is available and still matches the current PR.
- Repository enablement, independent SHA approval and model credential pass.
- No trusted bot report already exists for this head. Source/follower reruns
  do not automatically retry paid execution. Concurrent runs for a head are
  serialized using GitHub's existing concurrency mechanism.

Enabling this policy permits bounded eligible PR source/tests to be sent to
OpenAI without a separate per-PR data review. Confirm that repository data is
eligible for that disclosure. Existing credential-pattern rejection is a
heuristic, not a comprehensive committed-secret detector.

After testing analysis-only behavior or fixing configuration, use a new PR head
for the first enabled generation run. An existing bot report intentionally
prevents automatic spending again on the same head, including after a key or
enablement setting changes. Do not delete markers to create an unattended retry.

If the new environment requires reviewers, the GitHub-triggered generation job
waits for that approval. Fully automatic generation requires a reviewed policy
allowing the trusted main-branch job to run without per-run approval; no existing
protection is bypassed by this code. Do not enable this for unreviewed actors or
when the runner/image boundary is unsuitable. Secret absence, revoked approval,
stale commits or unavailable Docker fail closed and remain visible as blocked
or unavailable results, never AI success.

The fixed config reuses `demo_config(budget_usd=0.10)`: at most two model calls,
one repair after genuine failed verification, no model/service retries, bounded
context/output and conservative unknown-usage reservations. Spending estimates
are **per process/run**, not a durable global or account billing cap. Multiple
authorized PR events/new dispatches can spend multiple allowances. Previously
interrupted spending and a failed comment write cannot be accounted for across
new processes by this in-memory ledger; use provider-side controls and restrict
enablement accordingly. If that residual spending risk is unacceptable, leave
the flag unset: automatic secret-free analysis/reporting still works.

## PR results and reviewable patches

The PR comment contains decision/classification, full pinned base/head, generated
test paths, independent verification state/outcome, test counts, reported
statement/branch/line fractions, token usage, estimated cost and limitations.
Unknown metrics remain unavailable. No PR title, branch name, repository prose,
model rationale, prompt, raw console output or credential is rendered. Reports
from changed/closed/ambiguous PRs are not posted to a different/current commit.
GitHub has no atomic compare-head-and-comment operation: a head can advance
immediately after the final check, so every comment explicitly identifies its
exact revision and never represents a timeless/current-head attestation.

On verified success, the existing end-to-end runner can optionally export a
private **tests.patch** plus **manifest.json**. Export reuses ProposalApplier to
revalidate exact eligible paths/operations and binds original/result hashes,
proposal/policy digests, PR identity and patch SHA-256. No-op/binary or suspicious
credential-containing output is rejected. Nothing is written to either checkout
and no Git commit is made. The patch is uploaded as
`taskly-verified-tests-HEAD_SHA` for three days, as untrusted code for human
review. No publisher executes it or pushes to contributor branches. Missing
patch export/upload cannot be advertised as a completed successful workflow;
independent passing evidence is retained separately from that workflow failure.

For review, download the artifact, inspect its manifest and test contents, verify
its SHA-256, and use `git apply --check` on a separate checkout of the recorded
head. Applying/committing/pushing is an explicit human action outside this agent.
Do not execute an artifact merely because its manifest is structurally valid.

## Validation and remaining deployment checks

Deterministic tests use real temporary Git histories plus fake GitHub, Responses
HTTP and Docker. No automated test calls OpenAI. Before enabling the key, a human
must land the implementation and validate on GitHub:

1. With AI disabled, open/update a frontend-only PR; verify automatic pinned analysis
   and its sanitized PR comment after the normal baseline finishes.
2. Verify a fork/untrusted-author PR is analysis-only. GitHub sometimes omits
   source-run PR associations for forks: ambiguous/missing associations fail
   closed with a job diagnostic and no PR write. A commit-associated PR is not
   used as a substitute because a different PR can reuse the same commit.
   Reporting on forks with no authoritative association remains unsupported.
   Confirm actual GitHub metadata behavior on this repository.
3. Configure/approve the new environment and enable generation for one trusted
   frontend PR. Confirm actual API usage, real Docker counts/coverage, correct
   patch digest/application scope, and a matching PR comment/artifact. No such
   paid test was run during implementation.
4. Update the head while a run is in flight; confirm stale publication is
   suppressed. Check missing key, failed verification, artifact delivery and
   denied comment permissions produce explicit unavailable/failed results.

For the M5B hosted check, after human review lands this version on protected
`main`, independently set `TASKLY_PR_AGENT_BASE_SHA` to that exact reviewed SHA
in `taskly-pr-agent`; keep its environment-only key and branch/approval rules.
Start with `TASKLY_PR_AI_ENABLED` unset/false. Create a same-repository,
non-draft PR by an authorized author containing one small observable change
to `frontend/src/components/ApplicationHeader.tsx`, with unchanged dependency
manifests and no backend changes. For example, add an optional subtitle prop
whose default preserves the current `Task list` behavior, so the existing
baseline tests still pass; the generated tests should cover the new custom
subtitle behavior. A PR that already fails baseline tests remains ineligible
under the unchanged authorization policy. Confirm frontend classification and the
analysis-only comment. After explicit paid-run authorization, enable the flag
and make a new reviewed PR head (an existing comment intentionally prevents
spending again on its head). Wait for the successful first-attempt baseline
and any environment approval. Check that the admitted classification is
frontend, the real sandbox runs Vitest, evidence binds that head and policy,
and the matching comment/artifact reports counts, coverage and bounded usage
(two calls/one repair/$0.10 estimated maximum). Download the test-only patch,
check its manifest/hash, and apply/check it only in a disposable checkout of
the recorded head. Disable the flag after the authorized check. Repeat with a
new small `.ts` utility PR only if separately authorized; the M4C manual backend
workflow is unchanged. No real frontend model, Docker or GitHub execution was
performed during deterministic implementation tests.

Publication is one non-retried API write. One bot comment per head avoids spam;
if a PR has 100 or more comments, this minimal implementation fails closed rather
than implementing unbounded pagination. Network failure, publication permissions,
missing artifacts and real runner/API/model availability still need hosted
validation. No coverage improvement is claimed without compatible baseline
measurements. Aggregate passing results do not establish assertion quality;
report forgery inside a sandbox, the shared kernel, and trusted upstream
package/image/admin configuration remain the existing documented limitations.

Official references: [workflow_run privileges and warnings](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run),
[workflow-run metadata](https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run).
