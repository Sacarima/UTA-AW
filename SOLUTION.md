# Taskly Unit Test Agent

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

Use Bash, Git at `/usr/bin/git`, and Python 3.12 (CI uses 3.12.10).
Run from a reviewed repository root. Installing dependencies needs package-index
access; the tests and default transport smoke check need no API key, Docker,
Node, PostgreSQL, or external service.

```bash
python3.12 -m venv agent/.venv
agent/.venv/bin/python -m pip install -e './agent[test]'
agent/.venv/bin/python -m pytest agent/tests -q
agent/.venv/bin/python -I -m taskly_agent.openai_smoke
git diff --check
```

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

These are the recorded results supplied for this submission; remote records
were not re-fetched during documentation preparation.

| Example | Evidence and observed result |
| --- | --- |
| Backend AI generation | [PR #2](https://github.com/Sacarima/UTA-AW/pull/2), [live run 40297677678](https://github.com/Sacarima/UTA-AW/actions/runs/40297677678): **22 backend tests passed**, one model call, zero repairs, **$0.00538875 estimated cost**. Statement coverage remained **64/145 (44.1%)**, unchanged. |
| Automatic analysis/reporting | [PR #3](https://github.com/Sacarima/UTA-AW/pull/3): baseline verification and automatic PR-agent workflow passed. Documentation changes were classified `irrelevant`; decision `skip`, generation status `analysis_skipped`. GitHub Actions bot published a PR comment. No AI generation was needed. |
| Agent tests after M5B | **1,052 tests passed under Python 3.12.10**, including **45 frontend integration cases**. These are agent tests, not application tests; HTTP and Docker are faked in integration tests. |

## 6. Assumptions and limitations

- **Hosted frontend AI generation and automatic paid generation remain unverified.**
  Reporting success on a documentation PR proves neither. Paid generation needs
  explicit GitHub configuration and a passing baseline; already-failing PRs do
  not enter generation. Mixed frontend/backend generation is unsupported.
- No measured application coverage improvement is claimed. Reported coverage
  is a suite observation, not a compatible before/after comparison or proof of
  meaningful assertions. Generated patches are published for review, not
  automatically applied, merged, or pushed.
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
report-integrity checks, and reproducible dependency verification. Measure
compatible baseline/generated coverage before claiming improvement. Mixed-stack
support and broader context discovery need explicit tests and cost bounds;
stronger VM isolation is appropriate where hostile-code risk requires it.
