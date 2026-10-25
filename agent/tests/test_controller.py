import asyncio

import pytest
from pydantic import ValidationError

from taskly_agent.contracts import (
    AgentDecision, ChangedFile, ContentLimits, CoverageEvidence, CoverageMetric,
    CoverageMetrics, ModelUsage, PRContext, PRIdentity, ReviewResult,
    ReviewerFinding, RunLimits, RunResult, TestCounts as Counts,
    TestFileChange as FileChange, TestProposal as Proposal,
    VerificationEvidence, VerificationIdentity,
)
from taskly_agent.controller import ExecutionController
from taskly_agent.lifecycle import (
    ExecutionPolicy, ExecutionReport, GenerationResponse, InvalidTransition,
    Lifecycle, ReviewResponse, RunInput, proposal_digest,
)


def run_input(review=False):
    return RunInput(pr=PRContext(
        identity=PRIdentity(repository="Sacarima/UTA-AW", number=1,
                            base_sha="a" * 40, head_sha="b" * 40),
        changed_files=(ChangedFile(path="backend/app/api.py", status="modified"),),
    ), review_requested=review)


def usage(cost=0.01):
    return ModelUsage(model="fake", input_tokens=10, output_tokens=10, estimated_cost_usd=cost)


class Fakes:
    """One deterministic fixture implements the four separate protocols."""

    def __init__(self, outcomes=("passed",)):
        self.order = []
        self.outcomes = iter(outcomes)
        self.action = "generate"
        self.classification = "backend"
        self.failure = None
        self.error = RuntimeError("sensitive component details")
        self.mismatch = None
        self.cost = 0.01
        self.feedback = []
        self.evidence = []
        self.content = "assert True"

    def step(self, name):
        self.order.append(name)
        if self.failure == name:
            raise self.error

    async def analyze(self, request, policy):
        self.step("analyze")
        return AgentDecision(classification=self.classification, action=self.action, rationale="Analysis decision")

    async def generate(self, request, plan, policy, *, previous, feedback):
        self.step("repair" if previous is not None else "generate")
        self.feedback.append((previous, feedback))
        proposal = Proposal.model_validate({
            "rationale": "Cover behavior",
            "changes": (FileChange(path="backend/tests/test_api.py", operation="create", content=self.content),),
        })
        return GenerationResponse(proposal=proposal, usage=usage(self.cost))

    async def verify(self, request, proposal, policy):
        self.step("verify")
        outcome = next(self.outcomes)
        if outcome is None:
            return None
        if outcome in {"missing", "unavailable"}:
            evidence = VerificationEvidence(
                state=outcome, outcome="incomplete", reason="No report",
                coverage=CoverageEvidence(state=outcome, reason="No coverage report"),
            )
        else:
            identity = dict(repository=request.pr.identity.repository, head_sha=request.pr.identity.head_sha,
                            proposal_digest=proposal_digest(proposal), policy_digest=policy.digest)
            if self.mismatch:
                identity[self.mismatch] = "Other/repository" if self.mismatch == "repository" else "0" * len(identity[self.mismatch])
            evidence = VerificationEvidence(
                state="available", outcome=outcome, identity=VerificationIdentity(**identity),
                tests=Counts(passed=1, failed=int(outcome == "failed"), skipped=0, errors=0),
                coverage=CoverageEvidence(state="available", metrics=CoverageMetrics(
                    statements=None, branches=None, lines=CoverageMetric(covered=1, total=1),
                )),
            )
        self.evidence.append(evidence)
        return evidence

    async def review(self, request, proposal, policy):
        self.step("review")
        return ReviewResponse(review=ReviewResult(findings=(
            ReviewerFinding(severity="error", message="Advisory finding"),
        )), usage=usage(self.cost))


def execute(fake, *, request=None, policy=None, reviewer=True):
    controller = ExecutionController(analyzer=fake, generator=fake, verifier=fake,
                                     reviewer=fake if reviewer else None)
    return asyncio.run(controller.run(request or run_input(), policy or ExecutionPolicy()))


def test_successful_execution_order_and_structured_round_trip():
    fake = Fakes()
    report = execute(fake)
    assert fake.order == ["analyze", "generate", "verify"]
    assert report.history == ("initialized", "analyzed", "planned", "generated", "verified", "completed")
    assert report.result.outcome == "passed"
    assert report.result.verification == fake.evidence[-1]
    assert report.model_calls == 1
    assert ExecutionReport.model_validate_json(report.model_dump_json()) == report


@pytest.mark.parametrize("action,classification,outcome", [
    ("skip", "irrelevant", "skipped"), ("block", "backend", "blocked"),
])
def test_analysis_stops_before_generation(action, classification, outcome):
    fake = Fakes()
    fake.action, fake.classification = action, classification
    report = execute(fake)
    assert fake.order == ["analyze"]
    assert report.result.outcome == outcome
    assert report.result.verification is None
    assert report.model_calls == 0
    assert report.history == ("initialized", "analyzed", outcome)


def test_verification_failure_without_repair_is_failed():
    fake = Fakes(("failed",))
    report = execute(fake, policy=ExecutionPolicy(limits=RunLimits(max_repair_attempts=0)))
    assert report.result.outcome == "failed"
    assert report.reason_code == "verification_failed"
    assert report.result.verification == fake.evidence[0]
    assert fake.order == ["analyze", "generate", "verify"]


@pytest.mark.parametrize("outcome,reason", [(None, "missing_evidence"), ("missing", "missing_evidence"),
                                           ("unavailable", "unavailable_evidence")])
def test_missing_verification_never_succeeds_or_repairs(outcome, reason):
    fake = Fakes((outcome,))
    report = execute(fake)
    assert report.result.outcome == "incomplete"
    assert report.reason_code == reason
    assert report.model_calls == 1
    assert report.result.verification == (None if outcome is None else fake.evidence[0])


def test_reviewer_is_optional_and_advisory():
    fake = Fakes()
    report = execute(fake, request=run_input(review=True))
    assert fake.order == ["analyze", "generate", "verify", "review"]
    assert report.history[-2:] == ("reviewed", "completed")
    assert report.result.outcome == "passed"
    assert report.result.review.findings[0].severity == "error"
    assert report.result.verification == fake.evidence[0]
    assert report.model_calls == 2


def test_requested_but_missing_reviewer_preserves_verified_pass():
    fake = Fakes()
    report = execute(fake, request=run_input(review=True), reviewer=False)
    assert report.result.outcome == "blocked"
    assert report.result.verification.outcome == "passed"


def test_one_repair_receives_independent_feedback_and_is_reverified():
    fake = Fakes(("failed", "passed"))
    report = execute(fake)
    assert fake.order == ["analyze", "generate", "verify", "repair", "verify"]
    assert fake.feedback[0] == (None, None)
    assert fake.feedback[1][1] == fake.evidence[0]
    assert report.result.outcome == "passed"
    assert report.model_calls == 2 and report.repair_attempts == 1
    assert report.verification_history == tuple(fake.evidence)
    assert report.history[-4:] == ("verified", "generated", "verified", "completed")


def test_failed_repair_cannot_loop_or_invoke_reviewer():
    fake = Fakes(("failed", "failed"))
    report = execute(fake, request=run_input(review=True))
    assert report.result.outcome == "failed"
    assert report.reason_code == "repair_limit"
    assert fake.order.count("repair") == 1
    assert "review" not in fake.order


@pytest.mark.parametrize("review,outcomes", [(False, ("failed",)), (True, ("passed",))])
def test_model_call_exhaustion_preserves_verification(review, outcomes):
    fake = Fakes(outcomes)
    # Generation consumes the sole slot; no repair configured in this policy.
    policy = ExecutionPolicy(limits=RunLimits(max_model_calls=1, max_repair_attempts=0))
    report = execute(fake, request=run_input(review), policy=policy)
    assert report.result.outcome == ("blocked" if review else "failed")
    assert report.result.verification == fake.evidence[0]
    assert report.model_calls == 1
    if review:
        assert report.reason_code == "model_call_limit"
        assert "review" not in fake.order


def test_call_limit_blocks_review_after_successful_repair():
    # Two slots allow a repair, then block optional review while keeping its pass.
    fake = Fakes(("failed", "passed"))
    report = execute(fake, request=run_input(True), policy=ExecutionPolicy(limits=RunLimits(max_model_calls=2)))
    assert report.reason_code == "model_call_limit"
    assert report.result.verification.outcome == "passed"
    assert report.model_calls == 2


@pytest.mark.parametrize("field", ["repository", "head_sha", "proposal_digest", "policy_digest"])
def test_mismatched_verification_identity_is_not_accepted(field):
    fake = Fakes()
    fake.mismatch = field
    report = execute(fake)
    assert report.result.outcome == "blocked"
    assert report.reason_code == "identity_mismatch"
    assert report.result.verification is None
    assert report.verification_history == ()


@pytest.mark.parametrize("stage", ["analyze", "generate", "verify", "review"])
def test_component_exceptions_never_pass_or_expose_exception_text(stage):
    fake = Fakes()
    fake.failure = stage
    report = execute(fake, request=run_input(True))
    assert report.reason_code == "component_failure"
    assert report.result.outcome == "failed"
    assert "sensitive" not in report.result.summary
    assert (report.result.verification is not None) == (stage == "review")
    if stage == "review":
        assert report.result.verification.outcome == "passed"


@pytest.mark.parametrize("error,code", [(TimeoutError(), "timeout"), (asyncio.CancelledError(), "cancelled")])
def test_timeout_and_cancellation_preserve_existing_verification(error, code):
    fake = Fakes()
    fake.failure, fake.error = "review", error
    report = execute(fake, request=run_input(True))
    assert report.result.outcome == "incomplete"
    assert report.reason_code == code
    assert report.result.verification.outcome == "passed"


def test_actual_task_cancellation_returns_incomplete_without_evidence():
    async def scenario():
        started = asyncio.Event()
        fake = Fakes()

        async def analyze(request, policy):
            started.set()
            await asyncio.Event().wait()

        fake.analyze = analyze
        controller = ExecutionController(analyzer=fake, generator=fake, verifier=fake)
        task = asyncio.create_task(controller.run(run_input(), ExecutionPolicy()))
        await started.wait()
        task.cancel()
        return await task

    report = asyncio.run(scenario())
    assert report.reason_code == "cancelled"
    assert report.result.verification is None


def test_deadlines_are_applied_without_waiting_for_wall_clock(monkeypatch):
    deadlines = []

    async def timeout(operation, timeout):
        deadlines.append(timeout)
        operation.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", timeout)
    report = execute(Fakes())
    assert report.reason_code == "timeout"
    assert deadlines == [120]


def test_invalid_external_input_is_distinct_and_invokes_nothing():
    fake = Fakes()
    controller = ExecutionController(analyzer=fake, generator=fake, verifier=fake)
    report = asyncio.run(controller.run({"pr": "invalid"}, ExecutionPolicy()))
    assert report.reason_code == "invalid_input"
    assert report.result.outcome == "blocked"
    assert fake.order == []


@pytest.mark.parametrize("stage", ["analyze", "generate", "verify", "review"])
def test_invalid_component_outputs_are_rejected(stage):
    fake = Fakes()

    async def invalid(*args, **kwargs):
        return {"passed": True}

    setattr(fake, stage, invalid)
    report = execute(fake, request=run_input(True))
    assert report.reason_code == "invalid_component_output"
    assert report.result.outcome == "failed"
    if stage == "review":
        assert report.result.verification.outcome == "passed"


def test_budget_is_reserved_before_invocation():
    fake = Fakes()
    report = execute(fake, policy=ExecutionPolicy(limits=RunLimits(estimated_budget_usd=0.01)))
    assert report.reason_code == "budget_exhausted"
    assert fake.order == ["analyze"]
    assert report.model_calls == 0


@pytest.mark.parametrize("cost,reason", [(None, "unknown_cost"), (0.06, "budget_exhausted")])
def test_unknown_or_excessive_reported_cost_stops_execution(cost, reason):
    fake = Fakes()
    fake.cost = cost
    report = execute(fake)
    assert report.reason_code == reason
    assert fake.order == ["analyze", "generate"]
    assert report.result.outcome != "passed"
    assert report.result.usage[0].estimated_cost_usd == cost


@pytest.mark.parametrize("outcome", ["blocked", "skipped", "incomplete"])
def test_nonpassing_verifier_outcomes_are_preserved(outcome):
    report = execute(Fakes((outcome,)))
    assert report.result.outcome == outcome
    assert report.reason_code == f"verification_{outcome}"


def test_controller_runs_do_not_share_counters():
    fake = Fakes(("passed", "passed"))
    controller = ExecutionController(analyzer=fake, generator=fake, verifier=fake)
    reports = [asyncio.run(controller.run(run_input(), ExecutionPolicy())) for _ in range(2)]
    assert [report.model_calls for report in reports] == [1, 1]


def test_policy_rejects_more_than_one_repair_and_changes_identity():
    with pytest.raises(ValidationError):
        ExecutionPolicy(limits=RunLimits(max_model_calls=3, max_repair_attempts=2))
    assert ExecutionPolicy().digest != ExecutionPolicy(estimated_call_ceiling_usd=0.02).digest


def test_overall_pass_without_verification_is_impossible():
    with pytest.raises(ValidationError):
        RunResult(outcome="passed", summary="No evidence")


@pytest.mark.parametrize("target", ["verified", "completed", "unknown"])
def test_invalid_transition_is_rejected_without_changing_state(target):
    lifecycle = Lifecycle()
    with pytest.raises(InvalidTransition):
        lifecycle.transition(target)
    assert lifecycle.history == ("initialized",)


@pytest.mark.parametrize("terminal", ["failed", "skipped", "blocked", "incomplete"])
def test_terminal_states_cannot_be_resumed(terminal):
    lifecycle = Lifecycle()
    lifecycle.transition(terminal)
    with pytest.raises(InvalidTransition):
        lifecycle.transition("analyzed")


def test_repair_exception_retains_previous_independent_failure_evidence():
    fake = Fakes(("failed",))
    fake.failure = "repair"
    report = execute(fake)
    assert report.reason_code == "component_failure"
    assert report.model_calls == 2 and report.repair_attempts == 1
    assert report.result.verification == fake.evidence[0]
    assert report.verification_history == tuple(fake.evidence)


def test_controller_honors_custom_content_limits_through_final_report():
    fake = Fakes()
    content = "x" * (ContentLimits().max_test_content_bytes + 1)

    async def generate(request, plan, policy, **kwargs):
        # Raw boundary data is revalidated using the trusted policy, including
        # during report construction rather than reverting to default limits.
        return {
            "proposal": {"rationale": "Large but permitted", "changes": ({
                "path": "backend/tests/test_api.py", "operation": "create", "content": content,
            },)},
            "usage": usage(),
        }

    fake.generate = generate
    report = execute(fake, policy=ExecutionPolicy(content_limits=ContentLimits(max_test_content_bytes=len(content))))
    assert report.result.outcome == "passed"
    assert report.proposal.changes[0].content == content


def test_unknown_reviewer_cost_preserves_verified_pass_but_not_workflow_success():
    fake = Fakes()
    verify = fake.verify

    async def mark_cost_unknown(*args):
        result = await verify(*args)
        fake.cost = None
        return result

    fake.verify = mark_cost_unknown
    report = execute(fake, request=run_input(True))
    assert report.reason_code == "unknown_cost"
    assert report.result.outcome == "incomplete"
    assert report.result.verification.outcome == "passed"


def test_completed_lifecycle_cannot_transition_again():
    lifecycle = Lifecycle()
    for state in ("analyzed", "planned", "generated", "verified", "completed"):
        lifecycle.transition(state)
    with pytest.raises(InvalidTransition):
        lifecycle.transition("failed")


@pytest.mark.parametrize("stage", ["generate", "repair", "review"])
@pytest.mark.parametrize("error,reason", [
    (RuntimeError("unknown consumed tokens"), "component_failure"),
    (TimeoutError(), "timeout"), (asyncio.CancelledError(), "cancelled"),
])
def test_failed_or_interrupted_calls_keep_unknown_usage_reservations(stage, error, reason):
    fake = Fakes(("failed",) if stage == "repair" else ("passed",))
    fake.failure, fake.error = stage, error
    report = execute(fake, request=run_input(stage == "review"))
    assert report.reason_code == reason
    assert report.result.outcome != "passed"
    assert len(report.model_call_accounting) == report.model_calls
    interrupted = report.model_call_accounting[-1]
    assert interrupted.component == ("reviewer" if stage == "review" else "generator")
    assert interrupted.reserved_cost_usd == 0.05
    assert interrupted.reported_cost_usd is None
    assert len(report.result.usage) == report.model_calls - 1
    restored = ExecutionReport.model_validate_json(report.model_dump_json())
    assert restored.model_call_accounting == report.model_call_accounting
    if stage == "review":
        assert report.result.verification.outcome == "passed"


@pytest.mark.parametrize("stage", ["generate", "review"])
def test_actual_model_task_cancellation_retains_reservation(stage):
    async def scenario():
        started = asyncio.Event()
        fake = Fakes()

        async def interrupted(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        setattr(fake, stage, interrupted)
        controller = ExecutionController(analyzer=fake, generator=fake, verifier=fake, reviewer=fake)
        task = asyncio.create_task(controller.run(run_input(stage == "review"), ExecutionPolicy()))
        await started.wait()
        task.cancel()
        return await task

    report = asyncio.run(scenario())
    assert report.reason_code == "cancelled"
    assert report.model_call_accounting[-1].reserved_cost_usd == 0.05
    assert report.model_call_accounting[-1].reported_cost_usd is None


def test_reported_cost_replaces_reservation_without_double_counting():
    fake = Fakes()
    fake.cost = 0.1
    verify = fake.verify

    async def reviewer_cost(*args):
        evidence = await verify(*args)
        fake.cost = 0.2
        return evidence

    fake.verify = reviewer_cost
    policy = ExecutionPolicy(estimated_call_ceiling_usd=0.2,
                             limits=RunLimits(estimated_budget_usd=0.3))
    report = execute(fake, request=run_input(True), policy=policy)
    # Both calls fit only if the first reservation is reconciled. Decimal
    # accounting also permits the exact 0.1 + 0.2 == 0.3 budget boundary.
    assert report.result.outcome == "passed"
    assert [call.reserved_cost_usd for call in report.model_call_accounting] == [0.2, 0.2]
    assert [call.reported_cost_usd for call in report.model_call_accounting] == [0.1, 0.2]


@pytest.mark.parametrize("cost", [None, 0.0, 0.06])
def test_unknown_zero_and_over_ceiling_usage_remain_distinguishable(cost):
    fake = Fakes()
    fake.cost = cost
    report = execute(fake)
    call = report.model_call_accounting[0]
    assert call.reserved_cost_usd == 0.05
    assert call.reported_cost_usd == cost
    assert report.result.outcome == ("passed" if cost == 0.0 else "incomplete" if cost is None else "blocked")


@pytest.mark.parametrize("review", [False, True])
def test_budget_exhaustion_prevents_additional_reservations_and_invocations(review):
    fake = Fakes(("passed",) if review else ("failed",))
    fake.cost = 0.02
    policy = ExecutionPolicy(limits=RunLimits(estimated_budget_usd=0.06))
    report = execute(fake, request=run_input(review), policy=policy)
    assert report.reason_code == "budget_exhausted"
    assert report.model_calls == len(report.model_call_accounting) == 1
    assert report.model_call_accounting[0].reported_cost_usd == 0.02
    assert "repair" not in fake.order and "review" not in fake.order


def test_invalid_model_response_retains_unknown_usage_reservation():
    fake = Fakes()

    async def invalid(*args, **kwargs):
        return {"passed": True}

    fake.generate = invalid
    report = execute(fake)
    assert report.reason_code == "invalid_component_output"
    assert report.model_call_accounting[0].reported_cost_usd is None
    assert report.model_call_accounting[0].reserved_cost_usd == 0.05


@pytest.mark.parametrize("form", ["absent", "null", "malformed", "unvalidated_instance"])
def test_passing_verification_requires_identity_even_for_adapter_outputs(form):
    fake = Fakes()
    verify = fake.verify

    async def invalid_identity(*args):
        evidence = await verify(*args)
        if form == "unvalidated_instance":
            return evidence.model_copy(update={"identity": None})
        payload = evidence.model_dump()
        if form == "absent":
            del payload["identity"]
        elif form == "null":
            payload["identity"] = None
        else:
            payload["identity"]["head_sha"] = "not-a-commit"
        return payload

    fake.verify = invalid_identity
    report = execute(fake)
    assert report.reason_code == "invalid_component_output"
    assert report.result.outcome == "failed"
    assert report.result.verification is None
    assert report.verification_history == ()


@pytest.mark.parametrize("severity", [None, "warning", "error"])
def test_clean_review_and_serious_findings_are_visible_without_overriding_verification(severity):
    fake = Fakes()

    async def review(*args):
        findings = () if severity is None else (
            ReviewerFinding(severity=severity, message="Additional assertion recommended",
                            path="backend/tests/test_api.py", line=1),
        )
        return ReviewResponse(review=ReviewResult(findings=findings), usage=usage())

    fake.review = review
    report = execute(fake, request=run_input(True))
    restored = ExecutionReport.model_validate_json(report.model_dump_json())
    assert restored.result.review is not None
    assert restored.result.verification == fake.evidence[0]
    assert restored.result.verification.outcome == restored.result.outcome == "passed"
    if severity is None:
        assert restored.result.review.findings == ()
    else:
        finding, = restored.result.review.findings
        assert finding.severity == severity
        assert finding.message == "Additional assertion recommended"
        assert finding.path == "backend/tests/test_api.py" and finding.line == 1
