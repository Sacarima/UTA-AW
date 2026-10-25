"""Deterministic change impact, using Taskly naming conventions, not imports.

Inspect commit objects only. Matching tests are references, never proof of
coverage or write authorization. Renames consider both old and new names;
removed sources get cleanup notes, never new-source generation targets.
"""

import asyncio
from collections.abc import Collection
from pathlib import PurePosixPath
from typing import Literal

from .contracts import AgentDecision, ChangedFile, Contract
from .git_changes import GitAnalysisError, GitReader, validate_commit
from .lifecycle import ExecutionPolicy, RunInput
from .policy import PathPolicyError, validate_test_path
from .skills import SkillError, load_bundled_skills, select_skill_ids


class TestImpact(Contract):
    change: ChangedFile
    existing_tests: tuple[str, ...]
    suggested_test: str | None
    recommendation: Literal["add_tests", "review_tests", "skip_removed"]


class ChangeAnalysis(Contract):
    changes: tuple[ChangedFile, ...]
    impacts: tuple[TestImpact, ...]
    decision: AgentDecision


def eligible_test_paths(impacts: tuple[TestImpact, ...], available_tests: Collection[str]) -> dict[str, str]:
    """Deterministic proposal scope, shared by adapters and isolated application.

    Adapters supply only tests whose contents are in context; the writer supplies
    the pinned regular-file inventory. Missing original tests never become create
    permissions. These candidates still require strict path and filesystem checks.
    """
    paths = {}
    for impact in impacts:
        if impact.recommendation == "skip_removed":
            continue
        paths.update((path, "modify") for path in impact.existing_tests if path in available_tests)
        suggested = impact.suggested_test
        if suggested is not None:
            if suggested in available_tests:
                paths[suggested] = "modify"
            elif suggested not in impact.existing_tests:
                paths[suggested] = "create"
    return paths


def _component(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    if any(part in {"generated", "__generated__", "dist", "build", "coverage", "node_modules",
                       "test", "tests", "__tests__", "fixtures", "__fixtures__", "__mocks__"}
           for part in parts[2:]):
        return None
    name = parts[-1]
    if path.startswith("backend/app/") and path.endswith(".py"):
        return "backend"
    if path.startswith("frontend/src/") and path.endswith((".ts", ".tsx")):
        if name in {"mocks.ts", "mocks.tsx", "types.ts"} or name.endswith((
            ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx", ".stories.ts", ".stories.tsx",
            ".d.ts", ".types.ts", ".generated.ts", ".generated.tsx", ".gen.ts",
        )):
            return None
        return "frontend"
    return None


def _test_paths(source: str) -> tuple[str, ...]:
    path = PurePosixPath(source)
    if source.startswith("backend/app/"):
        relative = path.relative_to("backend/app")
        return (str(PurePosixPath("backend/tests") / relative.parent / f"test_{path.stem}.py"),
                f"backend/tests/test_{path.stem}.py")
    stem = path.with_suffix("")
    suffixes = ("test.tsx", "test.ts", "spec.tsx", "spec.ts") if path.suffix == ".tsx" else (
        "test.ts", "test.tsx", "spec.ts", "spec.tsx")
    return tuple(str(parent / f"{stem.name}.{suffix}")
                 for parent in (stem.parent, stem.parent / "__tests__")
                 for suffix in suffixes)


class GitChangeAnalyzer:
    def __init__(self, reader: GitReader, *, base_sha: str, head_sha: str):
        # Pinned by trusted caller, not selected by PR filenames or metadata.
        self.reader = reader
        self.base_sha, self.head_sha = validate_commit(base_sha), validate_commit(head_sha)

    def inspect(self) -> ChangeAnalysis:
        changes = ()
        try:
            changes = self.reader.changes(self.base_sha, self.head_sha)
            files = self.reader.files(self.head_sha)
            components, impacts = set(), []
            for change in changes:
                old = change.previous_path
                current_component = _component(change.path)
                previous_component = _component(old) if old else None
                components.update(filter(None, (current_component, previous_component)))
                if not (current_component or previous_component):
                    continue
                removed = change.status == "removed" or current_component is None
                candidates = tuple(dict.fromkeys(
                    candidate for source in (change.path, old)
                    if source is not None and _component(source)
                    for candidate in _test_paths(source)
                ))
                existing = tuple(path for path in candidates if files.get(path) in {"100644", "100755"})
                if not removed and files.get(change.path) not in {"100644", "100755"}:
                    raise GitAnalysisError("non_regular_source")
                suggested = None
                if not removed:
                    for candidate in _test_paths(change.path):
                        if candidate in files and files[candidate] not in {"100644", "100755"}:
                            continue
                        try:
                            suggested = validate_test_path(candidate)
                            break
                        except PathPolicyError:
                            continue
                impacts.append(TestImpact(
                    change=change, existing_tests=existing, suggested_test=suggested,
                    recommendation="skip_removed" if removed else "review_tests" if existing else "add_tests",
                ))
            classification = "mixed" if len(components) > 1 else next(iter(components), "irrelevant")
            active = [impact for impact in impacts if impact.recommendation != "skip_removed"]
            if active:
                details = []
                for impact in active[:10]:
                    detail = f"{impact.change.status}: {impact.change.path}: "
                    detail += ("review/update convention-matched tests" if impact.existing_tests
                               else "no convention-matched tests; add focused behavior tests if needed")
                    if impact.change.status == "renamed":
                        detail += "; check old-name test imports after rename"
                    if impact.suggested_test is None:
                        detail += "; select a policy-compliant test filename manually"
                    details.append(detail)
                rationale = f"{len(active)} production target(s). " + "; ".join(details)
                if len(impacts) > len(active):
                    rationale += "; removed sources need cleanup review, not new-source tests"
                rationale += ". Naming matches do not establish coverage or complete dependency impact."
                action = "generate"
            else:
                rationale = ("Only removed/moved-out production sources; review remaining tests for cleanup. "
                             "No surviving source target for generation." if impacts else
                             "No supported production-code changes; tests, configuration, generated files and documentation do not require automatic test generation.")
                action = "skip"
            decision = AgentDecision(classification=classification, action=action, rationale=rationale)
            if action == "generate":
                try:
                    selected = select_skill_ids(decision, tuple(impacts), load_bundled_skills())
                    decision = AgentDecision(**{**decision.model_dump(), "selected_skills": selected})
                except SkillError:
                    decision = AgentDecision(classification=classification, action="block",
                                             rationale="Bundled skills are missing, unsafe, or invalid")
            return ChangeAnalysis(changes=changes, impacts=tuple(impacts), decision=decision)
        except GitAnalysisError as error:
            return ChangeAnalysis(changes=changes, impacts=(), decision=AgentDecision(
                classification="irrelevant", action="block", rationale=f"Git analysis blocked: {error}",
            ))

    async def analyze(self, request: RunInput, policy: ExecutionPolicy) -> AgentDecision:
        request = RunInput.model_validate(request)
        if (request.pr.identity.base_sha, request.pr.identity.head_sha) != (self.base_sha, self.head_sha):
            return AgentDecision(classification="irrelevant", action="block",
                                 rationale="PR identity does not match the trusted commit range")
        # Git subprocesses have their own hard deadlines, even if this await is
        # cancelled. A thread avoids blocking the controller's event loop.
        return (await asyncio.to_thread(self.inspect)).decision
