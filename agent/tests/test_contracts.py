import json

import pytest
from pydantic import ValidationError

from taskly_agent.contracts import (
    AgentDecision,
    ChangedFile,
    ContentLimits,
    CoverageEvidence,
    CoverageMetric,
    CoverageMetrics,
    ModelUsage,
    PRContext,
    PRIdentity,
    ReviewerFinding,
    ReviewResult,
    RunLimits,
    RunResult,
    TestCounts as Counts,
    TestFileChange as FileChange,
    TestProposal as Proposal,
    VerificationEvidence,
    VerificationIdentity,
)


def coverage():
    return CoverageEvidence(
        state="available",
        metrics=CoverageMetrics(
            statements=None, branches=None, lines=CoverageMetric(covered=8, total=10)
        ),
    )


def verification_identity():
    return VerificationIdentity(
        repository="Sacarima/UTA-AW", head_sha="a" * 40,
        proposal_digest="b" * 64, policy_digest="c" * 64,
        artifact_refs=("tests/results.xml", "coverage/coverage.json"),
    )


def evidence(outcome="passed"):
    return VerificationEvidence(
        state="available", outcome=outcome, identity=verification_identity(),
        tests=Counts(passed=1, failed=0, skipped=0, errors=0), coverage=coverage(),
    )


def test_pr_context_accepts_real_changes_including_protected_analysis_paths():
    identity = PRIdentity(
        repository="Sacarima/UTA-AW", number=1, base_sha="a" * 40, head_sha="b" * 40
    )
    context = PRContext(identity=identity, changed_files=(
        ChangedFile(path=".github/workflows/ci.yml", status="modified"),
        ChangedFile(path=".gitignore", status="modified"),
        ChangedFile(path="backend/app/new.py", status="renamed", previous_path="backend/app/old.py"),
    ))
    assert PRContext.model_validate_json(context.model_dump_json()) == context


@pytest.mark.parametrize("field,value", [
    ("repository", "https://github.com/Sacarima/UTA-AW"),
    ("repository", "../.."),
    ("number", "1"), ("number", True), ("number", 0),
    ("head_sha", "main"), ("base_sha", "a" * 41),
])
def test_invalid_pr_identity(field, value):
    payload = dict(repository="Sacarima/UTA-AW", number=1, base_sha="a" * 40, head_sha="b" * 40)
    payload[field] = value
    with pytest.raises(ValidationError):
        PRIdentity.model_validate(payload)


@pytest.mark.parametrize("classification", ["frontend", "backend", "mixed", "irrelevant"])
def test_classification_and_skills_round_trip(classification):
    decision = AgentDecision.model_validate_json(json.dumps({
        "classification": classification, "rationale": "Changed application files",
        "action": "skip" if classification == "irrelevant" else "generate",
        "selected_skills": ["python-unit-tests", "react-unit-tests"],
    }))
    assert decision.classification == classification
    assert decision.selected_skills == ("python-unit-tests", "react-unit-tests")
    assert AgentDecision.model_validate_json(decision.model_dump_json()) == decision


@pytest.mark.parametrize("overrides", [
    {"classification": "fullstack"}, {"rationale": "  "}, {"rationale": 1},
    {"action": "execute"}, {"classification": "irrelevant", "action": "generate"},
    {"selected_skills": ["../skill"]}, {"selected_skills": ["Uppercase"]},
    {"selected_skills": ["a--b"]}, {"selected_skills": ["a", "a"]},
    {"passed": True},
])
def test_decision_rejects_invalid_or_unknown_fields(overrides):
    payload = {"classification": "mixed", "action": "generate", "rationale": "Tests needed", **overrides}
    with pytest.raises(ValidationError):
        AgentDecision.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("payload", [
    {"path": "backend/app/a.py", "status": "renamed"},
    {"path": "backend/app/a.py", "status": "modified", "previous_path": "backend/app/b.py"},
    {"path": "backend/app/a.py", "status": "renamed", "previous_path": "backend/app/a.py"},
    {"path": "../escape.py", "status": "added"},
    {"path": "backend/app/a.py", "status": "deleted"},
])
def test_invalid_changed_files(payload):
    with pytest.raises(ValidationError):
        ChangedFile.model_validate(payload)


def test_proposal_supports_create_and_modify_and_preserves_content():
    proposal = Proposal(rationale="Cover error handling", changes=(
        FileChange(path="backend/tests/test_new.py", operation="create", content="assert True\n"),
        FileChange(path="frontend/src/App.test.tsx", operation="modify", content="// existing test\n"),
    ))
    assert proposal.changes[1].operation == "modify"
    assert Proposal.model_validate_json(proposal.model_dump_json()) == proposal
    assert proposal.changes[0].content.endswith("\n")


@pytest.mark.parametrize("overrides", [
    {"path": "backend/app/main.py"}, {"operation": "delete"},
    {"content": " "}, {"content": 42}, {"passed": True},
])
def test_proposal_boundary_rejects_unsafe_or_invalid_data(overrides):
    payload = {"path": "backend/tests/test_new.py", "operation": "create", "content": "assert True", **overrides}
    with pytest.raises(ValidationError):
        FileChange.model_validate(payload)


def test_empty_and_case_alias_proposals_are_rejected():
    with pytest.raises(ValidationError):
        Proposal(rationale="Empty", changes=())
    for paths in [("a", "a"), ("A", "a")]:
        with pytest.raises(ValidationError):
            Proposal(rationale="Duplicate", changes=tuple(
                FileChange(path=f"frontend/src/{name}.test.ts", operation="create", content="test()")
                for name in paths
            ))


def test_model_proposals_cannot_embed_verification_evidence():
    payload = {
        "rationale": "Claimed success",
        "changes": [{"path": "backend/tests/test_a.py", "operation": "create", "content": "assert True"}],
        "verification": json.loads(evidence().model_dump_json()),
    }
    with pytest.raises(ValidationError):
        Proposal.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("state", ["missing", "unavailable"])
def test_missing_verification_cannot_be_a_pass(state):
    absent = CoverageEvidence(state=state, reason="No report")
    valid = VerificationEvidence(state=state, outcome="incomplete", coverage=absent, reason="No execution")
    assert valid.tests is None
    with pytest.raises(ValidationError):
        VerificationEvidence(state=state, outcome="passed", coverage=absent, reason="No execution")
    with pytest.raises(ValidationError):
        VerificationEvidence(state=state, outcome="incomplete", coverage=absent)


@pytest.mark.parametrize("counts", [
    Counts(passed=0, failed=0, skipped=0, errors=0),
    Counts(passed=0, failed=0, skipped=2, errors=0),
    Counts(passed=1, failed=1, skipped=0, errors=0),
    Counts(passed=1, failed=0, skipped=0, errors=1),
])
def test_empty_failed_or_errored_tests_never_pass(counts):
    with pytest.raises(ValidationError):
        VerificationEvidence(state="available", outcome="passed", identity=verification_identity(),
                             tests=counts, coverage=coverage())


def test_missing_coverage_is_incomplete_even_when_tests_pass():
    payload = dict(state="available", identity=verification_identity(),
                   tests=Counts(passed=1, failed=0, skipped=0, errors=0),
                   coverage=CoverageEvidence(state="missing", reason="Report not generated"))
    assert VerificationEvidence(outcome="incomplete", **payload).coverage.metrics is None
    with pytest.raises(ValidationError):
        VerificationEvidence(outcome="passed", **payload)


def test_verification_rejects_inconsistent_availability_and_unknown_outcomes():
    payload = json.loads(evidence().model_dump_json())
    for overrides in [{"tests": None}, {"identity": None}, {"outcome": "success"},
                      {"state": "missing", "reason": "Absent"}]:
        with pytest.raises(ValidationError):
            VerificationEvidence.model_validate_json(json.dumps({**payload, **overrides}))


@pytest.mark.parametrize("payload", [
    {"state": "available"},
    {"state": "missing"},
    {"state": "unavailable", "reason": "No report", "metrics": {
        "statements": None, "branches": None, "lines": {"covered": 1, "total": 1}}},
])
def test_coverage_availability_requires_consistent_data(payload):
    with pytest.raises(ValidationError):
        CoverageEvidence.model_validate_json(json.dumps(payload))


def test_invalid_coverage_and_counts_are_rejected():
    for payload in [{"covered": 2, "total": 1}, {"covered": -1, "total": 1}, {"covered": True, "total": 1}]:
        with pytest.raises(ValidationError):
            CoverageMetric.model_validate(payload)
    with pytest.raises(ValidationError):
        CoverageMetrics(statements=None, branches=None, lines=None)
    with pytest.raises(ValidationError):
        Counts(passed="1", failed=0, skipped=0, errors=0)
    assert CoverageMetric(covered=0, total=0).total == 0


def test_reviewer_cannot_replace_verification_or_supply_a_verdict():
    review = ReviewResult(findings=())
    with pytest.raises(ValidationError):
        RunResult(outcome="passed", summary="Reviewer liked it", verification=evidence("failed"), review=review)
    with pytest.raises(ValidationError):
        ReviewResult.model_validate_json('{"findings": [], "passed": true}')
    result = RunResult(outcome="passed", summary="Verified", verification=evidence(), review=ReviewResult(findings=(
        ReviewerFinding(severity="error", message="Consider another assertion"),
    )))
    assert result.verification.outcome == "passed"


@pytest.mark.parametrize("outcome", ["passed", "failed", "skipped", "blocked", "incomplete"])
def test_structured_outcomes_serialize_and_round_trip(outcome):
    result = RunResult(outcome=outcome, summary="Run recorded", verification=evidence(outcome))
    serialized = result.model_dump_json()
    assert json.loads(serialized)["outcome"] == outcome
    assert RunResult.model_validate_json(serialized) == result


def test_findings_require_meaningful_locations_and_messages():
    for payload in [{"severity": "error", "message": " "},
                    {"severity": "error", "message": "Missing assertion", "line": 1},
                    {"severity": "error", "message": "Unsafe", "path": "../x"}]:
        with pytest.raises(ValidationError):
            ReviewerFinding.model_validate(payload)


@pytest.mark.parametrize("value", [-1.0, float("inf"), float("nan"), "0.25", True])
def test_usage_rejects_invalid_estimated_cost(value):
    with pytest.raises(ValidationError):
        ModelUsage(model="mock", estimated_cost_usd=value)


def test_unknown_usage_stays_unknown_and_limits_are_validated():
    usage = ModelUsage(model="mock")
    assert json.loads(usage.model_dump_json())["estimated_cost_usd"] is None
    assert RunLimits().estimated_budget_usd == 0.25
    for overrides in [{"max_model_calls": 0}, {"max_repair_attempts": 3},
                      {"command_timeout_seconds": "120"}, {"max_input_tokens": -1}]:
        with pytest.raises(ValidationError):
            RunLimits(**overrides)


def test_reported_model_usage_round_trips_without_coercion():
    usage = ModelUsage(model="mock", request_id="request-1", input_tokens=25,
                       output_tokens=10, estimated_cost_usd=0.01)
    assert ModelUsage.model_validate_json(usage.model_dump_json()) == usage
    with pytest.raises(ValidationError):
        ModelUsage(model="mock", input_tokens="25")


def test_validated_contracts_are_immutable():
    result = evidence()
    with pytest.raises(ValidationError):
        result.outcome = "failed"
    with pytest.raises(ValidationError):
        result.tests.passed = 0


@pytest.mark.parametrize("workflow_outcome", ["blocked", "incomplete"])
def test_passing_verification_survives_publication_failure(workflow_outcome):
    result = RunResult(
        outcome=workflow_outcome, summary="Publication could not complete",
        verification=evidence(),
        review=ReviewResult(findings=(ReviewerFinding(severity="error", message="Advisory"),)),
    )
    restored = RunResult.model_validate_json(result.model_dump_json())
    assert restored.outcome == workflow_outcome
    assert restored.verification.outcome == "passed"


@pytest.mark.parametrize("verification_outcome", ["failed", "skipped", "blocked", "incomplete"])
@pytest.mark.parametrize("review", [None, ReviewResult(findings=())])
def test_overall_pass_requires_passing_verification(verification_outcome, review):
    with pytest.raises(ValidationError, match="overall pass requires independent verification"):
        RunResult(outcome="passed", summary="Claimed success",
                  verification=evidence(verification_outcome), review=review)


def test_verification_identity_round_trips_as_claims_without_authentication_flag():
    identity = verification_identity()
    assert VerificationIdentity.model_validate_json(identity.model_dump_json()) == identity
    assert evidence().identity == identity
    payload = json.loads(identity.model_dump_json())
    # Supplying a plausible identity does not authenticate its source.
    with pytest.raises(ValidationError):
        VerificationIdentity.model_validate_json(json.dumps({**payload, "authenticated": True}))


@pytest.mark.parametrize("field,value", [
    ("repository", "../UTA-AW"), ("repository", "https://github.com/Sacarima/UTA-AW"),
    ("head_sha", "main"), ("head_sha", 123),
    ("proposal_digest", "short"), ("proposal_digest", "B" * 64),
    ("policy_digest", "g" * 64), ("policy_digest", None),
    ("artifact_refs", ["../results.xml"]), ("artifact_refs", ["/tmp/results.xml"]),
    ("artifact_refs", [123]),
])
def test_invalid_verification_identity_is_rejected(field, value):
    payload = json.loads(verification_identity().model_dump_json())
    payload[field] = value
    with pytest.raises(ValidationError):
        VerificationIdentity.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("field", ["repository", "head_sha", "proposal_digest", "policy_digest"])
def test_identity_fields_are_required(field):
    payload = json.loads(verification_identity().model_dump_json())
    del payload[field]
    with pytest.raises(ValidationError):
        VerificationIdentity.model_validate_json(json.dumps(payload))


def test_metadata_paths_preserve_spaces_unicode_and_normalization():
    path = "frontend/src/Área de trabalho/cafe\u0301.tsx"
    changed = ChangedFile(path=path, status="renamed", previous_path="frontend/src/旧 文件.tsx")
    finding = ReviewerFinding(severity="warning", message="Review behavior", path=path, line=1)
    assert ChangedFile.model_validate_json(changed.model_dump_json()).path == path
    assert ReviewerFinding.model_validate_json(finding.model_dump_json()).path == path
    with pytest.raises(ValidationError):
        FileChange(path="frontend/src/Área de trabalho/a.test.tsx", operation="create", content="test()")


def test_default_content_limit_uses_utf8_bytes_and_preserves_exact_boundary():
    limit = ContentLimits().max_test_content_bytes
    for content in ("x" * limit, "é" * (limit // 2)):
        assert FileChange(path="backend/tests/test_a.py", operation="create", content=content).content == content
    for content in ("x" * (limit + 1), "é" * (limit // 2 + 1)):
        with pytest.raises(ValidationError, match="byte limit"):
            FileChange(path="backend/tests/test_a.py", operation="create", content=content)


def test_proposal_file_count_limit_is_enforced_at_boundary():
    limit = ContentLimits().max_proposed_files
    changes = [{"path": f"backend/tests/test_{i}.py", "operation": "create", "content": "assert True"}
               for i in range(limit + 1)]
    assert len(Proposal.model_validate_json(json.dumps({"rationale": "Bounded", "changes": changes[:limit]})).changes) == limit
    with pytest.raises(ValidationError, match="file-count limit"):
        Proposal.model_validate_json(json.dumps({"rationale": "Too many", "changes": changes}))


def test_review_finding_limit_is_enforced_at_boundary():
    limit = ContentLimits().max_review_findings
    findings = [{"severity": "info", "message": "Suggestion"}] * (limit + 1)
    assert len(ReviewResult.model_validate_json(json.dumps({"findings": findings[:limit]})).findings) == limit
    with pytest.raises(ValidationError, match="finding-count limit"):
        ReviewResult.model_validate_json(json.dumps({"findings": findings}))


def test_trusted_context_configures_all_content_limits_including_nested_models():
    limits = ContentLimits(max_test_content_bytes=4, max_proposed_files=1, max_review_findings=1)
    context = {"content_limits": limits}
    change = {"path": "backend/tests/test_a.py", "operation": "create", "content": "éé"}
    assert Proposal.model_validate_json(json.dumps({"rationale": "Fits", "changes": [change]}), context=context)
    with pytest.raises(ValidationError, match="byte limit"):
        Proposal.model_validate_json(json.dumps({"rationale": "Too large", "changes": [{**change, "content": "ééx"}]}), context=context)
    with pytest.raises(ValidationError, match="file-count limit"):
        Proposal.model_validate_json(json.dumps({"rationale": "Too many", "changes": [change, change]}), context=context)
    findings = [{"severity": "info", "message": "Advice"}] * 2
    with pytest.raises(ValidationError, match="finding-count limit"):
        RunResult.model_validate_json(json.dumps({
            "outcome": "passed", "summary": "Verified", "verification": json.loads(evidence().model_dump_json()),
            "review": {"findings": findings},
        }), context=context)
    # Existing instances are revalidated under the receiving boundary's limits.
    existing = FileChange(**{**change, "content": "longer"})
    with pytest.raises(ValidationError, match="byte limit"):
        Proposal.model_validate({"rationale": "Revalidate", "changes": (existing,)}, context=context)


def test_limits_are_trusted_configuration_not_model_output():
    change = {"path": "backend/tests/test_a.py", "operation": "create", "content": "assert True"}
    with pytest.raises(ValidationError):
        Proposal.model_validate_json(json.dumps({
            "rationale": "Override", "changes": [change], "content_limits": {"max_proposed_files": 999},
        }))
    for value in (0, -1, True, "10"):
        with pytest.raises(ValidationError):
            ContentLimits(max_proposed_files=value)
