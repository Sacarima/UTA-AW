"""Pinned, read-only source context. No prompts, provider calls or file writes.

Repository text (including comments, tests and patches) is untrusted data. A
future prompt builder must preserve that boundary rather than treating source
text as instructions. Only the bundled skill loader supplies trusted guidance.
Naming conventions are bounded hints, not a dependency graph or secret scanner.
"""

from collections.abc import Callable
import json
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, ValidationError, model_validator

from .analysis import GitChangeAnalyzer, TestImpact
from .config import AgentConfig
from .contracts import AgentDecision, ChangedFile, CommitSHA, Contract, Count, PRIdentity
from .git_changes import GitAnalysisError
from .lifecycle import RunInput
from .provider import ProviderRequest
from .policy import validate_repository_path
from .skills import Skill, SkillError, load_bundled_skills, select_skill_ids


class ContextLimits(Contract):
    max_files: Annotated[int, Field(gt=0)] = 20
    max_file_bytes: Annotated[int, Field(gt=0)] = 16_384
    max_context_bytes: Annotated[int, Field(gt=0)] = 32_768
    max_diff_bytes: Annotated[int, Field(gt=0)] = 16_384
    include_diff: bool = True


class ContextFile(Contract):
    path: Annotated[str, AfterValidator(validate_repository_path)]
    blob_sha: CommitSHA
    role: Literal["source", "test", "interface", "fixture"]
    content: str  # Preserve whitespace, including empty source files.
    size_bytes: Count


class RepositoryContext(Contract):
    trust: Literal["untrusted"] = "untrusted"
    identity: PRIdentity
    decision: AgentDecision
    changes: tuple[ChangedFile, ...]
    impacts: tuple[TestImpact, ...]
    files: tuple[ContextFile, ...]
    unavailable_optional_paths: tuple[str, ...]
    diff_state: Literal["included", "not_requested"]
    diff: str | None


class SourceContext(Contract):
    repository_data: RepositoryContext
    trusted_skills: tuple[Skill, ...]

    def input_json(self) -> str:
        """Stable envelope; skill bodies and repository text never share a field."""
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"))


ContextReason = Literal[
    "ready", "analysis_skipped", "analysis_blocked", "identity_mismatch", "invalid_input",
    "missing_file", "unsafe_file", "binary_file", "file_limit", "file_bytes",
    "context_bytes", "diff_bytes", "git_error", "skills_error", "token_limit", "token_count_unavailable",
]


class ContextResult(Contract):
    state: Literal["ready", "skipped", "blocked"]
    reason: ContextReason
    path: str | None = None
    context: SourceContext | None = None
    context_bytes: Count | None = None
    input_tokens: Count | None = None

    @model_validator(mode="after")
    def complete_or_absent(self):
        available = (self.context, self.context_bytes, self.input_tokens)
        if self.state == "ready":
            if self.reason != "ready" or any(value is None for value in available):
                raise ValueError("ready context requires complete budget-checked data")
        elif any(value is not None for value in available):
            raise ValueError("failed/skipped collection cannot expose partial context")
        return self


class _Unavailable(Exception):
    def __init__(self, reason: ContextReason, path: str | None = None):
        self.reason, self.path = reason, path


def _nearby(source: str, tests: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """Fixed, shallow conventions only; never parse imports or source directives."""
    path = PurePosixPath(source)
    candidates = []
    if source.startswith("backend/app/"):
        candidates.extend((str(path.parent / name), "interface")
                          for name in ("schemas.py", "models.py", "types.py", "constants.py"))
        candidates.extend((("backend/tests/conftest.py", "fixture"), ("backend/tests/mocks.py", "fixture")))
        for test in tests:
            parent = PurePosixPath(test).parent
            candidates.append((str(parent / "conftest.py"), "fixture"))
            if parent.parent != PurePosixPath("backend"):
                candidates.append((str(parent.parent / "conftest.py"), "fixture"))
    else:
        for parent in (path.parent, path.parent.parent):
            if parent == PurePosixPath("frontend"):
                continue
            candidates.extend((str(parent / name), "interface") for name in ("types.ts", "schemas.ts", "constants.ts"))
        candidates.append((str(path.with_suffix(".types.ts")), "interface"))
        candidates.append(("frontend/src/test/setup.ts", "fixture"))
        if "/features/todos/" in source:
            candidates.append(("frontend/src/test/todoFixture.ts", "fixture"))
        if path.suffix == ".tsx":
            candidates.append(("frontend/src/design-system/mocks.tsx", "fixture"))
    return tuple(candidates)


class ContextCollector:
    """The required token counter must account for provider framing/schema.

    Budgets cover this exact serialized envelope. A later prompt builder must
    recheck the complete request if it adds instructions or other content.
    """

    def __init__(self, analyzer: GitChangeAnalyzer,
                 count_input_tokens: Callable[[ProviderRequest], int]):
        self.analyzer, self._count_tokens = analyzer, count_input_tokens

    def collect(self, request: RunInput, config: AgentConfig,
                limits: ContextLimits | None = None) -> ContextResult:
        try:
            request = RunInput.model_validate(request)
            config = AgentConfig.model_validate(config)
            limits = ContextLimits.model_validate(ContextLimits() if limits is None else limits)
        except ValidationError:
            return ContextResult(state="blocked", reason="invalid_input")
        if (request.pr.identity.base_sha, request.pr.identity.head_sha) != (
            self.analyzer.base_sha, self.analyzer.head_sha,
        ):
            return ContextResult(state="blocked", reason="identity_mismatch")
        try:
            analysis = self.analyzer.inspect()
            if analysis.decision.action != "generate":
                return ContextResult(state="skipped" if analysis.decision.action == "skip" else "blocked",
                                     reason="analysis_skipped" if analysis.decision.action == "skip" else "analysis_blocked")
            catalog = load_bundled_skills()
            selected = select_skill_ids(analysis.decision, analysis.impacts, catalog)
            if selected != analysis.decision.selected_skills:
                raise _Unavailable("skills_error")
            by_name = {skill.name: skill for skill in catalog}
            trusted = tuple(by_name[name] for name in selected)
            files = self.analyzer.reader.objects(self.analyzer.head_sha)
            chosen, missing = {}, set()

            def add(path, role, *, optional=False):
                if path not in files:
                    if optional:
                        missing.add(path)
                        return
                    raise _Unavailable("missing_file", path)
                if files[path].mode not in {"100644", "100755"}:
                    raise _Unavailable("unsafe_file", path)
                chosen.setdefault(path, role)
                if len(chosen) > limits.max_files:
                    raise _Unavailable("file_limit")

            # Primary files take precedence over supporting roles; removal notes
            # remain in changes/diff but deleted sources are never read at head.
            active = [impact for impact in analysis.impacts if impact.recommendation != "skip_removed"]
            for impact in active:
                add(impact.change.path, "source")
                for test in impact.existing_tests:
                    add(test, "test")
            for impact in active:
                for path, role in _nearby(impact.change.path, impact.existing_tests):
                    add(path, role, optional=True)

            context_files = []
            used = len(json.dumps([skill.model_dump() for skill in trusted], ensure_ascii=False).encode("utf-8"))
            byte_budget = min(limits.max_context_bytes, config.max_request_bytes)
            for path, role in chosen.items():
                if used >= byte_budget:
                    raise _Unavailable("context_bytes")
                try:
                    data = self.analyzer.reader.blob(files[path].object_id, max_bytes=limits.max_file_bytes)
                except GitAnalysisError as error:
                    raise _Unavailable("file_bytes" if str(error) in {"blob_limit", "output_limit"} else "git_error", path) from None
                try:
                    content = data.decode("utf-8", errors="strict")
                    if "\0" in content:
                        raise ValueError
                except ValueError:
                    raise _Unavailable("binary_file", path) from None
                used += len(data)
                if used > byte_budget:
                    raise _Unavailable("context_bytes", path)
                context_files.append(ContextFile(path=path, blob_sha=files[path].object_id,
                                                 role=role, content=content, size_bytes=len(data)))
            diff = None
            if limits.include_diff:
                relevant = set(chosen) | {impact.change.path for impact in analysis.impacts}
                paths = tuple(dict.fromkeys(path for change in analysis.changes
                    if change.path in relevant for path in (change.path, change.previous_path) if path is not None))
                try:
                    raw_diff = self.analyzer.reader.diff(self.analyzer.base_sha, self.analyzer.head_sha,
                                                        paths, max_bytes=limits.max_diff_bytes)
                except GitAnalysisError as error:
                    raise _Unavailable("diff_bytes" if str(error) == "output_limit" else "git_error") from None
                try:
                    diff = raw_diff.decode("utf-8", errors="strict")
                    if "\0" in diff:
                        raise ValueError
                except ValueError:
                    raise _Unavailable("binary_file") from None
            context = SourceContext(repository_data=RepositoryContext(
                identity=request.pr.identity, decision=analysis.decision,
                changes=analysis.changes, impacts=analysis.impacts,
                files=tuple(context_files),
                unavailable_optional_paths=tuple(sorted(missing)),
                diff_state="included" if limits.include_diff else "not_requested", diff=diff,
            ), trusted_skills=trusted)
            payload = context.input_json()
            size = len(payload.encode("utf-8"))
            if size > byte_budget:
                raise _Unavailable("context_bytes")
            policy = config.execution_policy.limits
            bounded_request = ProviderRequest(model=config.model, response_kind="test_proposal", input_json=payload,
                                             max_input_tokens=policy.max_input_tokens, max_output_tokens=policy.max_output_tokens)
            try:
                tokens = self._count_tokens(bounded_request)
            except Exception:
                raise _Unavailable("token_count_unavailable") from None
            if type(tokens) is not int or tokens < 0:
                raise _Unavailable("token_count_unavailable")
            if tokens > policy.max_input_tokens:
                raise _Unavailable("token_limit")
            return ContextResult(state="ready", reason="ready", context=context, context_bytes=size, input_tokens=tokens)
        except _Unavailable as error:
            return ContextResult(state="blocked", reason=error.reason, path=error.path)
        except SkillError:
            return ContextResult(state="blocked", reason="skills_error")
        except GitAnalysisError:
            return ContextResult(state="blocked", reason="git_error")
