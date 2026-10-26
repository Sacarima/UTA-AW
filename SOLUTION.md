# Taskly Unit Test Agent

## Quick Start — How to Use the Agent

1. **Clone and install.** Use Bash, Python 3.12, and `/usr/bin/git`.
   Install only reviewed code; dependency installation needs package-index access.

   ```bash
   git clone https://github.com/Sacarima/UTA-AW.git
   cd UTA-AW
   python3.12 -m venv agent/.venv
   agent/.venv/bin/python -m pip install -e './agent[test]'
   ```

2. **Verify locally without credentials.** No API key, Docker, Node, or database
   is needed. Integration tests use fakes; the smoke command makes no API call.

   ```bash
   agent/.venv/bin/python -m pytest agent/tests -q
   agent/.venv/bin/python -I -m taskly_agent.openai_smoke
   ```

3. **Receive automatic analysis.** In `Sacarima/UTA-AW`, open/update a PR targeting
   `main` with `TASKLY_PR_AI_ENABLED` unset/false. With the workflows on the default
   branch, Actions enabled, and PR-comment permission allowed, baseline completion
   triggers analysis and a matched PR comment without an OpenAI key. Documentation
   changes skip generation, as demonstrated on PR #3.

4. **Enable paid generation only after approval.** An administrator creates
   environment `taskly-pr-agent`, restricts it to protected `main`, sets its
   environment-only `OPENAI_API_KEY` secret and independently approved
   `TASKLY_PR_AGENT_BASE_SHA` variable, then sets repository variable
   `TASKLY_PR_AI_ENABLED=true`. The approved SHA must equal PR base and workflow
   commit. Retain required-reviewer protection as appropriate. Use a new reviewed
   head after enablement; an existing bot report suppresses repeat spending.
   Section 3 lists eligibility checks. **Automatic paid generation and hosted
   frontend generation remain unverified.**

5. **Retrieve and manually review generated tests.** Only after verified
   generation succeeds, follow the PR comment's Actions link and download/extract
   `taskly-verified-tests-<HEAD_SHA>` from **Artifacts** within its three-day
   retention. It contains `tests.patch` and `manifest.json`; skipped/failed
   generation has no verified patch. Review the patch as untrusted code and check
   the manifest's repository, head, verification identity, and `patch_sha256`
   against the successful run and the patch's SHA-256. Use a clean disposable
   checkout at that exact head. After those checks, apply manually:

   ```bash
   read -r -p 'Absolute extracted artifact directory: ' ARTIFACT_DIR
   read -r -p 'Absolute clean disposable checkout at verified head: ' REVIEW_CHECKOUT
   git -C "$REVIEW_CHECKOUT" apply --check "$ARTIFACT_DIR/tests.patch"
   git -C "$REVIEW_CHECKOUT" apply "$ARTIFACT_DIR/tests.patch"
   git -C "$REVIEW_CHECKOUT" diff --check
   git -C "$REVIEW_CHECKOUT" diff
   ```

   Generated tests are **never automatically committed, pushed, or merged**.
   Test execution still belongs in the documented sandbox, without credentials.

**Choose the right execution path:** the commands above test the agent locally.
Local end-to-end generation requires trusted Linux/Docker, pinned Git identities,
an immutable provisioned image, configuration, and explicit `--allow-paid`; follow
[END_TO_END.md](agent/END_TO_END.md). GitHub PR automation provisions and invokes
that same pipeline via workflows and protected environments; see
[PR_AUTOMATION.md](agent/PR_AUTOMATION.md). The local CLI emits reports; PR
integration enables patch export.

## 1. Overview and architecture

Taskly uses React/TypeScript/Vitest and Python/FastAPI/pytest. The separate
[agent/](agent/) package analyzes pinned PR changes, generates tests, independently
verifies them in Docker, and publishes sanitized, commit-bound results.
Backend-only and frontend-only generation share one pipeline; mixed generation
is blocked and irrelevant changes skip generation.

1. `GitChangeAnalyzer` reads bounded, NUL-delimited Git metadata for explicit
   base/head commits, classifies production changes, and finds convention-matched
   tests. It does not execute candidate code or infer complete dependencies.
2. `ContextCollector` reads pinned Git blobs, existing tests, nearby supporting
   files, and diffs within file/byte/token budgets. Repository text is untrusted
   reference data, separate from trusted instructions.
3. `ExecutionController` invokes `GeneratorAdapter`, the budgeted OpenAI Responses
   provider, and `VerifierAdapter`. One repair is allowed only after independent,
   available failed verification. An advisory reviewer exists but is disabled
   in the bounded end-to-end configuration. No Agents SDK or graph engine is used.
4. `ProposalApplier` authorizes paths and create/modify operations against analysis,
   then materializes the exact head in a private disposable workspace.
   `DockerSandbox` runs fixed pytest or Vitest commands and collects test/coverage
   reports. Verification identity binds repository, head, proposal, and policy.
5. PR automation publishes sanitized results. A verified proposal can produce
   `tests.patch` and `manifest.json`; export applies the patch to another disposable
   pinned baseline and checks file hashes, including unchanged files. Generated
   files are never pushed to contributor branches or written to the original checkout.

## 2. Custom agent and reusable skills

The implementation is under [agent/src/taskly_agent/](agent/src/taskly_agent/).
Entry points are [end_to_end.py](agent/src/taskly_agent/end_to_end.py) and
[pr_automation.py](agent/src/taskly_agent/pr_automation.py).
Strict Pydantic contracts separate untrusted proposals, trusted configuration,
review findings, verification evidence, and overall workflow outcomes. The
Responses API transport uses the official OpenAI Python SDK, not the Agents SDK.

The trusted registry loads only the four bundled `SKILL.md` files below; metadata,
size, directory contents, and identifiers are validated. PR files cannot supply skills.

- [python-unit-tests](agent/src/taskly_agent/bundled_skills/python-unit-tests/SKILL.md): pytest/FastAPI conventions.
- [react-unit-tests](agent/src/taskly_agent/bundled_skills/react-unit-tests/SKILL.md): Vitest, React Testing Library, jsdom, and isolated mocks.
- [update-existing-tests](agent/src/taskly_agent/bundled_skills/update-existing-tests/SKILL.md): preserve useful assertions when behavior changes.
- [review-generated-tests](agent/src/taskly_agent/bundled_skills/review-generated-tests/SKILL.md): assertion quality, isolation, duplication, and edge cases.

### Reproduce local verification

The Quick Start above provides the exact agent installation, test-suite, and
credential-free smoke commands. Run them from the reviewed repository root.

Agent dependencies stay separate from `backend/requirements.txt`. For Taskly's
own local test/coverage commands, see [README.md](README.md): frontend uses
`npm ci`, `npm test`, and `npm run test:coverage` from `frontend/`; backend uses
its own virtual environment and `python -m pytest --cov=app --cov-report=term-missing --cov-report=html`
from `backend/`. Frontend requires Node >=22.12; the sandbox pins Node 22.14.0.

## 3. PR automation workflow and prerequisites

| Workflow | Trigger and responsibility |
| --- | --- |
| [taskly-verification.yml](.github/workflows/taskly-verification.yml) | Secret-free `pull_request` baseline on opened, synchronize, reopened, and ready_for_review. |
| [taskly-live-demo.yml](.github/workflows/taskly-live-demo.yml) | Manual backend demonstration on `main`; requires explicit `allow_paid`, protected environment `taskly-live-demo`, independent `TASKLY_DEMO_BASE_SHA`, and environment secret `OPENAI_API_KEY`. |
| [taskly-pr-agent.yml](.github/workflows/taskly-pr-agent.yml) | Follows completion of `Taskly isolated verification` using `workflow_run`; analysis/generation have read-only GitHub access, while publication can write PR comments. |

Automatic generation requires repository variable `TASKLY_PR_AI_ENABLED=true`,
environment `taskly-pr-agent`, its environment-only `OPENAI_API_KEY`, and
environment variable `TASKLY_PR_AGENT_BASE_SHA` equal to both PR base and trusted workflow commit.
The PR must be open, non-draft, same-repository, target `main`, have an
OWNER/MEMBER/COLLABORATOR author, a supported generation decision, and a successful
first-attempt matching baseline. Forks/unauthorized authors cannot generate;
missing or ambiguous identity fails closed. First-attempt checks, head-based
serialization, and bot-report markers suppress ordinary reruns. Required environment
reviewers make generation approval-gated. See [PR_AUTOMATION.md](agent/PR_AUTOMATION.md).

For the manual backend workflow, dispatch on `main` with `base_sha`, `head_sha`,
`pr_number`, and `allow_paid=true`, only after approval. The independent approved
SHA must match the base and actual workflow commit. Its candidate check admits
only the specific demonstration delta; it is not a general PR generator.

Real verification requires trusted Linux amd64, cgroup v2, builtin seccomp,
`/usr/bin/docker`, and an immutable image built from reviewed base inputs. Image
construction needs registry/package access; candidate dependency manifests must
match the provisioned base. No host-test fallback exists. The complete Bash
provisioning, `$0.10` configuration, and execution procedure is in
[END_TO_END.md](agent/END_TO_END.md). Its entry point is
`python -I -m taskly_agent.end_to_end`, requiring `--repository`, `--name`,
`--number`, `--base`, `--head`, `--trusted-revision`, `--image`, and `--config`.
Paid execution additionally requires `--allow-paid` and `OPENAI_API_KEY` in the
trusted process environment. Without that flag, eligible changes return
`paid_execution_not_authorized` without calling OpenAI. The CLI emits reports;
PR automation enables the verified patch export hook, not a CLI patch flag.

## 4. Security, isolation, and cost controls

Trusted orchestration comes from reviewed commits; candidate code executes only
inside Docker. Containers receive no API key or GitHub token, network, host mounts,
or Docker socket; they run non-root with a read-only root and resource limits.
The model receives no execution tools. Writes are limited to analysis-authorized
`backend/tests/**/test_*.py` or `frontend/src/**/*.test.ts[x]`; production files,
fixtures, configuration, traversal, and unsafe links are rejected.

Publication rechecks current PR identity and exposes sanitized counts, coverage,
usage, estimated cost, and unavailable reasons. It never executes downloaded
artifacts. A successful verified generation run retains a test-only patch for human review, not
automatic application. Reports remain commit-bound because head-check/comment
publication is not atomic.

The workflows use at most two model calls, one repair, and $0.10 estimated
spending, with a $0.05 reservation per call. The provider has a 30-second timeout,
2,000 output-token limit, no automatic HTTP/model retries, and no model tools.
Unknown usage retains its reservation; reliable usage reconciles it once.
The generic `demo_config()` default is $3, so bounded runs explicitly use
`demo_config(budget_usd=0.10)`. Per-run and cumulative accounting track the same
calls and must not be added together. See [OPENAI_TRANSPORT.md](agent/OPENAI_TRANSPORT.md).

## 5. Verified examples and results

The GitHub results below are recorded demonstration evidence; remote records
were not re-fetched during documentation preparation. The subsequent application
coverage comparison was measured locally using unchanged coverage configurations.

| Example | Evidence and observed result |
| --- | --- |
| Backend AI generation | [PR #2](https://github.com/Sacarima/UTA-AW/pull/2), [live run 40297677678](https://github.com/Sacarima/UTA-AW/actions/runs/40297677678): **22 backend tests passed**, one model call, zero repairs, **$0.00538875 estimated cost**. Statement coverage remained **64/145 (44.1%)**, unchanged. |
| Automatic analysis/reporting | [PR #3](https://github.com/Sacarima/UTA-AW/pull/3): baseline verification and automatic PR-agent workflow passed. Documentation changes were classified `irrelevant`; decision `skip`, generation status `analysis_skipped`. GitHub Actions bot published a PR comment. No AI generation was needed. |
| Agent tests after M5B | **1,052 tests passed under Python 3.12.10**, including **45 frontend integration cases**. These are agent tests, not application tests; HTTP and Docker are faked in integration tests. |

### Measured application coverage improvement (M5D)

Four focused unit tests were manually added per application, independently of
the AI-generated backend demonstration above. Both complete application suites
passed with **zero failures or skips**, and frontend type checking passed.

| Application | Before statement coverage | After statement coverage | Tests passed, before → after |
| --- | --- | --- | --- |
| Backend | 64/145 (44.14%) | **85/145 (58.62%)** | 21 → **25** |
| Frontend | 78/168 (46.43%) | **90/168 (53.57%)** | 18 → **22** |

- [repository.py](backend/app/features/todos/repository.py): **18/39 → 39/39 statements**.
  Tests cover creation/persistence, partial updates including false/null values,
  missing records, and deletion in [test_repository.py](backend/tests/features/todos/test_repository.py).
- [http.ts](frontend/src/lib/http.ts): **0/12 → 12/12 statements**.
  Tests cover request options, JSON/204 responses, API/network errors, and
  cancellation in [http.test.ts](frontend/src/lib/http.test.ts).

With the application dependencies installed as described above, reproduce from
the repository root using the same commands used for both coverage measurements:

```bash
(cd backend && .venv/bin/python -m pytest --cov=app --cov-report=term-missing --cov-report=json --cov-report=html)
(cd frontend && npm run test:coverage)
(cd frontend && npm run typecheck)
```

Reports: `backend/coverage/coverage.json`, `backend/coverage/html/index.html`,
`frontend/coverage/coverage-summary.json`, and `frontend/coverage/index.html`.
Application code, dependency versions, and coverage exclusions/configuration
were unchanged. These measured gains are from manually added application tests;
they are not evidence of an AI-generated coverage increase.

## 6. Assumptions and limitations

- **Hosted frontend AI generation and automatic paid generation remain unverified.**
  Reporting success on a documentation PR proves neither. Paid generation needs
  explicit GitHub configuration and a passing baseline; already-failing PRs do
  not enter generation. Mixed frontend/backend generation is unsupported.
- The application coverage gains above do not establish an AI-generated coverage
  improvement, and coverage alone does not prove assertion quality. Generated
  patches are published for review, not automatically applied, merged, or pushed.
- Git naming conventions and bounded nearby context are not complete dependency
  analysis. Missing required context, runtime, dependencies, or evidence fail closed.
  Forks with missing/ambiguous PR associations cannot receive a matched report;
  comment histories of 100 or more entries also fail closed.
- Spending storage is process-local, not a durable cross-run/account cap.
  Interrupted runs or failed publication can permit repeated spending through a
  new execution. Provider-side controls remain separate.
- Prompt separation cannot guarantee injection resistance; secret detection is
  heuristic. Docker shares a kernel and assumes a trusted patched runner/daemon.
  Receipts bind capture provenance, not the honesty of adversarial test reports.
  See [CI_EXECUTION.md](agent/CI_EXECUTION.md).

## 7. Production improvements

Priorities are an authorized hosted frontend run and automatic paid-generation
validation, durable atomic spending/idempotency records, stronger assertion and
report-integrity checks, and reproducible dependency verification. For future AI
runs, compare compatible baseline/generated coverage before attributing gains
to the agent. Mixed-stack
support and broader context discovery need explicit tests and cost bounds;
stronger VM isolation is appropriate where hostile-code risk requires it.
