"""Strict data boundaries, not execution or authorization logic.

Parse external JSON with model_validate_json(). Collections are immutable
tuples in Python and arrays in JSON. Validated evidence is a claim whose
provenance must be supplied by the future trusted verifier, never the model.
Do not use model_construct() or unvalidated model_copy(update=...) on input.
"""

from typing import Annotated, Literal

from pydantic import (
    AfterValidator, BaseModel, ConfigDict, Field, StringConstraints,
    ValidationInfo, field_validator, model_validator,
)

from .policy import validate_repository_path, validate_test_path


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
CommitSHA = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
SHA256Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RepositoryName = Annotated[
    str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"),
    AfterValidator(validate_repository_path),
]
SkillIdentifier = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
]
Count = Annotated[int, Field(ge=0)]
EstimatedUSD = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Classification = Literal["frontend", "backend", "mixed", "irrelevant"]
RunOutcome = Literal["passed", "failed", "skipped", "blocked", "incomplete"]
EvidenceState = Literal["available", "missing", "unavailable"]


class Contract(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, validate_default=True,
        revalidate_instances="always",
    )


class ContentLimits(Contract):
    """Trusted input limits; never accept these settings from model output.

    Override via model_validate_json(..., context={"content_limits": limits}).
    Context propagates to nested changes and findings. Limits do not replace
    a transport-level cap on raw JSON bytes before parsing.
    """

    max_test_content_bytes: Annotated[int, Field(gt=0)] = 65_536
    max_proposed_files: Annotated[int, Field(gt=0)] = 10
    max_review_findings: Annotated[int, Field(gt=0)] = 50


_DEFAULT_CONTENT_LIMITS = ContentLimits()


def _content_limits(info: ValidationInfo) -> ContentLimits:
    limits = (info.context or {}).get("content_limits", _DEFAULT_CONTENT_LIMITS)
    if not isinstance(limits, ContentLimits):
        raise ValueError("content_limits context must be a trusted ContentLimits instance")
    return limits


class PRIdentity(Contract):
    repository: RepositoryName
    number: Annotated[int, Field(gt=0)]
    base_sha: CommitSHA
    head_sha: CommitSHA


class ChangedFile(Contract):
    path: str
    status: Literal["added", "modified", "removed", "renamed"]
    previous_path: str | None = None

    @field_validator("path", "previous_path")
    @classmethod
    def safe_path(cls, value: str | None) -> str | None:
        return validate_repository_path(value) if value is not None else None

    @model_validator(mode="after")
    def rename_has_origin(self):
        if (self.status == "renamed") != (self.previous_path is not None):
            raise ValueError("only renamed files require a previous_path")
        if self.previous_path == self.path:
            raise ValueError("renamed paths must differ")
        return self


class PRContext(Contract):
    identity: PRIdentity
    changed_files: tuple[ChangedFile, ...]


class AgentDecision(Contract):
    classification: Classification
    action: Literal["generate", "skip", "block"]
    rationale: NonEmptyText
    selected_skills: tuple[SkillIdentifier, ...] = ()

    @field_validator("selected_skills")
    @classmethod
    def unique_skills(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("selected skills must be unique")
        return value

    @model_validator(mode="after")
    def irrelevant_changes_are_skipped(self):
        if self.classification == "irrelevant" and self.action == "generate":
            raise ValueError("irrelevant changes cannot request test generation")
        return self


class TestFileChange(Contract):
    path: str
    operation: Literal["create", "modify"]
    content: Annotated[str, Field(min_length=1)]

    @field_validator("path")
    @classmethod
    def test_path(cls, value: str) -> str:
        return validate_test_path(value)

    @field_validator("content")
    @classmethod
    def nonblank_content(cls, value: str, info: ValidationInfo) -> str:
        if len(value.encode("utf-8")) > _content_limits(info).max_test_content_bytes:
            raise ValueError("generated test content exceeds the UTF-8 byte limit")
        if not value.strip():
            raise ValueError("test content must not be blank")
        return value


class TestProposal(Contract):
    rationale: NonEmptyText
    changes: Annotated[tuple[TestFileChange, ...], Field(min_length=1)]

    @field_validator("changes", mode="before")
    @classmethod
    def bounded_changes(cls, value, info: ValidationInfo):
        if isinstance(value, (tuple, list)) and len(value) > _content_limits(info).max_proposed_files:
            raise ValueError("proposal exceeds the file-count limit")
        # A before validator exposes JSON arrays as Python lists. Restore the
        # documented JSON-array/immutable-tuple boundary without coercing other
        # Python input types under strict validation.
        return tuple(value) if info.mode == "json" and isinstance(value, list) else value

    @field_validator("changes")
    @classmethod
    def unique_paths(cls, value: tuple[TestFileChange, ...]) -> tuple[TestFileChange, ...]:
        # Case-folding also prevents collisions on common macOS filesystems.
        paths = [change.path.casefold() for change in value]
        if len(set(paths)) != len(paths):
            raise ValueError("proposal paths must be unique, including case aliases")
        return value


class ReviewerFinding(Contract):
    severity: Literal["info", "warning", "error"]
    message: NonEmptyText
    path: str | None = None
    line: Annotated[int, Field(gt=0)] | None = None

    @field_validator("path")
    @classmethod
    def repository_path(cls, value: str | None) -> str | None:
        return validate_repository_path(value) if value is not None else None

    @model_validator(mode="after")
    def line_requires_path(self):
        if self.line is not None and self.path is None:
            raise ValueError("a finding line requires a path")
        return self


class ReviewResult(Contract):
    """Advisory findings only; deliberately has no verification verdict."""

    findings: tuple[ReviewerFinding, ...]

    @field_validator("findings", mode="before")
    @classmethod
    def bounded_findings(cls, value, info: ValidationInfo):
        if isinstance(value, (tuple, list)) and len(value) > _content_limits(info).max_review_findings:
            raise ValueError("review exceeds the finding-count limit")
        return tuple(value) if info.mode == "json" and isinstance(value, list) else value


class TestCounts(Contract):
    passed: Count
    failed: Count
    skipped: Count
    errors: Count


class CoverageMetric(Contract):
    """Keep integer counts; zero total means no applicable items, not 100%."""

    covered: Count
    total: Count

    @model_validator(mode="after")
    def bounded_coverage(self):
        if self.covered > self.total:
            raise ValueError("covered cannot exceed total")
        return self


class CoverageMetrics(Contract):
    # None explicitly means this metric was not reported.
    statements: CoverageMetric | None
    branches: CoverageMetric | None
    lines: CoverageMetric | None

    @model_validator(mode="after")
    def at_least_one_metric(self):
        if self.statements is None and self.branches is None and self.lines is None:
            raise ValueError("available coverage requires at least one metric")
        return self


class CoverageEvidence(Contract):
    state: EvidenceState
    metrics: CoverageMetrics | None = None
    reason: NonEmptyText | None = None

    @model_validator(mode="after")
    def explicit_availability(self):
        if self.state == "available":
            if self.metrics is None:
                raise ValueError("available coverage requires metrics")
        elif self.metrics is not None or self.reason is None:
            raise ValueError("missing/unavailable coverage requires a reason and no metrics")
        return self


class VerificationIdentity(Contract):
    """Identity claims, not authenticated or freshness-checked evidence.

    Digests are lowercase SHA-256 hex over the exact proposal/patch and trusted
    policy bytes. The future verifier/controller must compute and compare them
    with expected inputs and establish provenance; parsing cannot do that.
    Artifact references are relative to the run artifact root, not write grants.
    """

    repository: RepositoryName
    head_sha: CommitSHA
    proposal_digest: SHA256Digest
    policy_digest: SHA256Digest
    artifact_refs: tuple[str, ...] = ()

    @field_validator("artifact_refs")
    @classmethod
    def relative_artifacts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for reference in value:
            validate_repository_path(reference)
        return value


class VerificationEvidence(Contract):
    """Only the independent verifier may supply this contract to RunResult.

    Pass requires available tests and coverage. This foundation does not invent
    a coverage threshold; the future verifier must apply trusted acceptance
    policy before producing evidence. Missing coverage means incomplete, even
    when the recorded test counts themselves show no failures.
    """

    state: EvidenceState
    outcome: RunOutcome
    identity: VerificationIdentity | None = None
    tests: TestCounts | None = None
    coverage: CoverageEvidence
    reason: NonEmptyText | None = None

    @model_validator(mode="after")
    def evidence_supports_outcome(self):
        if self.state == "available":
            if self.tests is None or self.identity is None:
                raise ValueError("available verification requires identity and test counts")
        elif self.tests is not None or self.reason is None or self.coverage.state == "available":
            raise ValueError("missing/unavailable verification requires a reason and no results")
        if self.outcome == "passed":
            if (
                self.state != "available"
                or self.coverage.state != "available"
                or self.tests is None
                or self.tests.passed == 0
                or self.tests.failed != 0
                or self.tests.errors != 0
            ):
                raise ValueError("a verified pass requires executed, passing test evidence")
        return self


class ModelUsage(Contract):
    model: NonEmptyText
    request_id: NonEmptyText | None = None
    input_tokens: Count | None = None
    output_tokens: Count | None = None
    estimated_cost_usd: EstimatedUSD | None = None


class RunLimits(Contract):
    """Declared ceilings only; enforcement belongs to the future controller.

    USD values are estimates, not billing guarantees. None in ModelUsage means
    unknown cost and must never be treated as free by future budget enforcement.
    """

    max_model_calls: Annotated[int, Field(gt=0)] = 3
    max_repair_attempts: Count = 1
    command_timeout_seconds: Annotated[int, Field(gt=0)] = 120
    max_input_tokens: Annotated[int, Field(gt=0)] = 8000
    max_output_tokens: Annotated[int, Field(gt=0)] = 3000
    estimated_budget_usd: EstimatedUSD = 0.25

    @model_validator(mode="after")
    def repairs_fit_call_limit(self):
        if self.max_repair_attempts >= self.max_model_calls:
            raise ValueError("repair attempts must leave a call for initial generation")
        return self


class RunResult(Contract):
    """Workflow completion is separate from the immutable verifier verdict."""

    outcome: RunOutcome
    summary: NonEmptyText
    verification: VerificationEvidence | None = None
    review: ReviewResult | None = None
    usage: tuple[ModelUsage, ...] = ()

    @model_validator(mode="after")
    def pass_requires_verification(self):
        if self.outcome == "passed" and (
            self.verification is None or self.verification.outcome != "passed"
        ):
            raise ValueError("an overall pass requires independent verification to pass")
        return self
