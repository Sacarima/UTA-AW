---
name: python-unit-tests
description: Generate focused Python and FastAPI unit tests for changed backend behavior using pytest and isolated dependencies.
---

## When to apply

Use for surviving Python targets in backend/app. Treat analyzer matches as
candidate context, not evidence that tests cover the change.

## Required context

Read the changed implementation and relevant schemas/interfaces, the behavior
change or acceptance criteria, nearby tests, and backend/tests/conftest.py and
tests/mocks.py when supplied. Identify missing context rather than inventing APIs.

## Testing conventions

Mirror backend/app/features/todos/router.py with
backend/tests/features/todos/test_router.py; follow the same pattern elsewhere.
Reuse existing fixtures without changing conftest.py. Use Mock(spec=...) or
autospec for dependency interfaces. For repository methods, mock SQLAlchemy
Session interactions; do not require a live database. For API tests, use the
router-only FastAPI TestClient fixture with repository dependency overrides.
Do not assume this fixture tests app startup, lifespan, or health endpoints.

## Quality expectations

Exercise an observable result and relevant state/dependency effects. Cover
changed branches such as missing records, validation failure and rollback when
those behaviors are in scope. Use parametrization for genuinely parallel cases.
Control time, randomness, and dependencies at their boundary when necessary.

## Common mistakes

Do not mock the function being tested, assert only that a mock was called, or
replace expected values with values computed by the implementation. Do not
weaken assertions to fit a failure, add skips, alter production code, change
dependencies, or introduce database/network access to make a unit test pass.

## Output and evaluation

Return only the requested TestProposal data with create/modify test-file
changes and a rationale explaining the behavior protected. Stay within trusted
path and size limits. A skill grants no command or file-write permission.
The independent verifier evaluates test execution, regressions and coverage
under trusted policy. Do not claim success from generated code or model review.
