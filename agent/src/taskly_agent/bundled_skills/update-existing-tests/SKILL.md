---
name: update-existing-tests
description: Update existing behavioral tests when implementation changes or source renames affect their expectations and imports.
---

## When to apply

Use when analysis locates existing tests for a surviving source target. Prefer
focused modifications to duplicating equivalent tests. A naming match is only
a lead; inspect what the test actually exercises.

## Required context

Compare the old/new behavior or supplied diff, current source, existing test
contents, fixtures and relevant public interfaces. For renamed sources, inspect
old-name test imports and both paths. If behavior intent is unclear, report the
missing information instead of changing expectations to match an apparent bug.

## Testing conventions

Preserve the surrounding pytest or Vitest/React Testing Library style. Keep
fixtures, setup and dependency overrides intact. Keep independent unchanged
behavior assertions, and add a focused regression for the behavior change.
For source deletion, report cleanup needs; do not create tests importing a
removed source or delete tests through a create/modify-only proposal contract.

## Quality expectations

Explain which previous expectation is obsolete and why the replacement follows
the stated behavior. Prefer one cohesive test change over duplicate files.
Review negative paths and renamed imports, not only the happy path.

## Common mistakes

Do not delete assertions to obtain green tests, mark failures skipped/xfail,
blindly regenerate snapshots, broaden mocks to conceal regressions, or rewrite
global configuration. Existing .spec files and fixtures can be read as context
but are not automatically authorized write targets; obey the test-file policy.

## Output and evaluation

Return TestProposal create/modify changes only, describing retained guarantees
and the intended regression coverage. Do not claim coverage improved without
execution. The independent verifier must assess the updated and existing suite
under the same trusted configuration. Review cannot override that verdict.
