---
name: react-unit-tests
description: Generate React and TypeScript unit tests with Vitest and React Testing Library for changed frontend behavior.
---

## When to apply

Use for surviving frontend/src TypeScript or TSX behavior. Choose observable
component, hook, utility, or API-wrapper behavior rather than snapshots of structure.

## Required context

Read the target, related props/types, current tests, supplied API/query interfaces,
frontend/src/test/setup.ts, and nearby fixtures. Consult design-system mocks
when testing a feature component that already uses that convention.

## Testing conventions

Colocate tests as Name.test.ts or Name.test.tsx, following nearby files. Vitest
uses jsdom, jest-dom matchers, and global React Testing Library cleanup. Prefer
screen queries by role and accessible name; use userEvent.setup() and await
interactions. Use waitFor/findBy queries for asynchronous observable behavior.
Mock API or mutation boundaries with vi.mock and reset mocks between tests.
Use the existing todoFixture where appropriate. Supply an isolated QueryClient
only when a hook test requires one; avoid shared cache or retry-related delays.

## Quality expectations

Assert user-visible state, submitted data, navigation callbacks, or request
arguments that form the target's public behavior. Include relevant busy/error,
empty-state, validation and rejected-request cases. When testing the HTTP wrapper
itself, mock fetch rather than mocking the wrapper being tested.

## Common mistakes

Do not use real HTTP, arbitrary sleeps, unawaited interactions, implementation
selectors, or assertions that merely repeat mocked output. Do not change setup,
application code, lockfiles, coverage exclusions or dependencies. Do not replace
an existing behavioral test with a superficial render assertion.

## Output and evaluation

Return the requested TestProposal with bounded create/modify test-file changes.
When source names are outside the writable path profile, choose an allowed
test filename without renaming the source. Instructions grant no tool or write
permission. Independent execution and trusted coverage policy determine success;
passing-looking code and review findings are not verification evidence.
