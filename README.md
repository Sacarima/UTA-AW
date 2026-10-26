# Taskly Unit Test Agent (UTA-AW)

A custom AI unit-test agent for the Taskly application. It analyzes pinned pull
request changes, proposes focused tests, independently verifies them in Docker,
and publishes commit-bound results and reviewable test-only patches.

The agent is this repository's main deliverable. The supplied Taskly application
and its original setup/development instructions are preserved below.

## Key capabilities

- Custom Python orchestration using the OpenAI Responses API, bounded repair,
  and an optional advisory reviewer; verification remains independent.
- Four trusted, reusable `SKILL.md` capabilities selected deterministically.
- Python/FastAPI/pytest and React/TypeScript/Vitest/React Testing Library support
  for backend-only or frontend-only changes. Mixed generation is unsupported.
- Automatic GitHub PR analysis and sanitized comments without an OpenAI key;
  paid generation requires explicit configuration and eligibility checks.
- Isolated Docker test execution and coverage collection, with no credentials,
  network, host mounts, or host-test fallback for candidate code.
- Safe proposal application and patch export, bound to repository/head/proposal/
  policy identities, with a byte/hash round-trip check before export.

## How it works

**PR → deterministic analysis → optional AI generation → independent Docker
verification → PR report and, on verified success, a reviewable patch.**

The secret-free baseline workflow runs first. Irrelevant changes skip generation;
blocked or unavailable execution is reported without claiming success. Generated
files stay in disposable workspaces. Nothing is automatically committed,
pushed into contributor branches, or merged.

## Quick Start — credential-free verification

Use Bash, Python 3.12 (CI uses 3.12.10), and Git at `/usr/bin/git`. Install from
reviewed code; dependency installation needs package-index access. These commands
exercise the agent, not live model generation or Taskly's application tests:

```bash
git clone https://github.com/Sacarima/UTA-AW.git
cd UTA-AW
python3.12 -m venv agent/.venv
agent/.venv/bin/python -m pip install -e './agent[test]'
agent/.venv/bin/python -m pytest agent/tests -q
agent/.venv/bin/python -I -m taskly_agent.openai_smoke
```

No API key, Docker, Node, or database is required for these agent checks.
Integration tests fake HTTP and Docker; the default smoke command makes no API
call. The recorded M5B result was **1,052 passing agent tests**, including
**45 frontend integration cases**. These are not application test counts.

For **local end-to-end generation**, follow the full provisioning and execution
commands in [agent/END_TO_END.md](agent/END_TO_END.md). That path requires trusted
Linux amd64, cgroup v2, builtin seccomp, Docker, pinned base/head commits, a
separately built immutable image, trusted configuration, and explicit paid-run
approval. The entry point is `python -I -m taskly_agent.end_to_end`; `--allow-paid`
and a model credential are required to spend. The CLI emits reports; PR
integration enables patch export.

## Use with GitHub pull requests

1. **Start with analysis only.** In `Sacarima/UTA-AW`, open/update a PR targeting
   `main`, leaving repository variable `TASKLY_PR_AI_ENABLED` unset/false.
   Workflows must be on the default branch, Actions enabled, and PR-comment
   permissions permitted. [taskly-verification.yml](.github/workflows/taskly-verification.yml)
   runs the secret-free baseline; [taskly-pr-agent.yml](.github/workflows/taskly-pr-agent.yml)
   follows its completion and posts matched analysis. No OpenAI key is needed.
2. **Opt into paid generation after review.** Configure protected environment
   `taskly-pr-agent`, restricted to protected `main`, with environment-only secret
   `OPENAI_API_KEY` and independently approved variable `TASKLY_PR_AGENT_BASE_SHA`.
   The approved SHA must equal both PR base and trusted workflow commit. Then
   set repository variable `TASKLY_PR_AI_ENABLED=true`. Required reviewers can
   retain per-run approval. Never add the key to the baseline or publisher job.
3. **Meet eligibility checks.** Paid execution requires an open, non-draft,
   same-repository PR to `main`, OWNER/MEMBER/COLLABORATOR author association,
   supported production changes, successful first-attempt baseline, and current
   pinned identities. Forks cannot generate. Use a new reviewed head after
   enablement: an existing bot report suppresses repeat spending. Runs allow
   at most two model calls, one repair, and **$0.10 estimated cost**; accounting
   is process-local, not an account billing cap.
4. **Review generated artifacts.** After verified generation succeeds, follow
   the PR comment's Actions link and download `taskly-verified-tests-<HEAD_SHA>`
   within its three-day retention. Extract `tests.patch` and `manifest.json`;
   inspect the tests and verify the manifest's identity and SHA-256 against the
   successful run. Only then manually apply/check the patch in a clean disposable
   checkout at the recorded head. See [the review procedure](SOLUTION.md).
   Skipped/failed generation has no verified patch. The agent never commits or
   merges it for you.

**Automatic paid generation and hosted frontend AI generation remain unverified.**
The separate [manual backend demonstration workflow](.github/workflows/taskly-live-demo.yml)
uses `taskly-live-demo` and `TASKLY_DEMO_BASE_SHA`; it is not the automatic PR path.
Detailed eligibility, identity checks, and failure behavior are documented in
[agent/PR_AUTOMATION.md](agent/PR_AUTOMATION.md).

## Demonstrations

| Evidence | What it establishes |
| --- | --- |
| [PR #2](https://github.com/Sacarima/UTA-AW/pull/2) · [backend live run](https://github.com/Sacarima/UTA-AW/actions/runs/40297677678) | Real backend AI generation: **22 tests passed**, one model call, zero repairs; estimated cost **$0.00538875**. Statement coverage stayed **64/145 (44.1%)**, unchanged. |
| [PR #3](https://github.com/Sacarima/UTA-AW/pull/3) | Successful baseline, automatic analysis, and bot PR comment. Documentation changes were `irrelevant`; decision `skip`, status `analysis_skipped`. No AI generation was needed. |
| [PR #4](https://github.com/Sacarima/UTA-AW/pull/4) | Backend `generate` decision reached. The reported generation attempt was blocked as `repeat_attempt`, with **zero model calls**; the visible PR comment records `ai_disabled`. Neither record demonstrates successful generation. |

## Measured application coverage improvements

Four focused tests per application were written separately from the live AI
backend demonstration. The recorded M5D comparison used unchanged application
code and coverage settings:

| Application | Statement coverage before → after | Tests before → after |
| --- | --- | --- |
| Backend | **64/145 (44.14%) → 85/145 (58.62%)** | **21 → 25** |
| Frontend | **78/168 (46.43%) → 90/168 (53.57%)** | **18 → 22** |

Both complete application suites passed without failures or skips; frontend type
checking passed. `repository.py` increased from 18/39 to 39/39 covered statements;
`http.ts` increased from 0/12 to 12/12. These measured gains are from manually
added application tests, not AI-generated coverage improvement. Reproduction
commands and details are in [SOLUTION.md](SOLUTION.md).

## Project layout

```text
agent/src/taskly_agent/       Contracts, analysis, context, adapters, controller,
                             provider, application, verification, and PR integration
agent/src/taskly_agent/bundled_skills/
  python-unit-tests/SKILL.md
  react-unit-tests/SKILL.md
  update-existing-tests/SKILL.md
  review-generated-tests/SKILL.md
agent/sandbox/               Trusted Docker image and isolated launcher
agent/tests/                 Deterministic agent tests
.github/workflows/           Baseline, manual demonstration, and PR automation
backend/                     Original FastAPI application and pytest tests
frontend/                    Original React application and Vitest tests
```

## Documentation

- [SOLUTION.md](SOLUTION.md): architecture, evidence, measured coverage, and limitations.
- [agent/PR_AUTOMATION.md](agent/PR_AUTOMATION.md): GitHub setup, paid opt-in, reports, and artifact review.
- [agent/END_TO_END.md](agent/END_TO_END.md): trusted local image provisioning and generation commands.
- [agent/CI_EXECUTION.md](agent/CI_EXECUTION.md): Docker security boundary, prerequisites, and operational limitations.

## Original Taskly Application — Setup and Development

The instructions below come from [the original Taskly project](https://github.com/cf-recruitment/taskly-app).
Their text, commands, technical details, and links are unchanged; only the title
and heading levels were adjusted to fit this README. All agent documentation
above is new. The upstream README has no separate prerequisites or troubleshooting
section; none was removed. Run application commands from the directories specified.

### Setup

```sh
cp .env.example .env
```

### Docker

```sh
docker compose up --build -d --wait
```

Application: http://localhost:8080. API: http://localhost:8000/docs.

```sh
docker compose down
```

### Frontend

Installation:

```sh
cd frontend
npm ci
```

Development — http://localhost:5173:

```sh
npm run dev
```

Storybook — http://localhost:6006:

```sh
npm run storybook
```

Tests:

```sh
npm test
```

Coverage — `frontend/coverage/index.html`:

```sh
npm run test:coverage
```

### Backend

Installation (from the repository root):

```sh
python3 -m venv backend/.venv
source backend/.venv/bin/activate
python -m pip install -r backend/requirements.txt
```

Development — http://localhost:8000/docs:

```sh
docker compose up -d --wait postgres
cd backend
python -m uvicorn app.main:app --reload
```

Tests (in `backend`, with `.venv` activated):

```sh
python -m pytest
```

Coverage — `backend/coverage/html/index.html`:

```sh
python -m pytest --cov=app --cov-report=term-missing --cov-report=html
```
