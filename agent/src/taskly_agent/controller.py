"""In-memory orchestration only: no files, model clients, subprocesses or GitHub.

The injected verifier is trusted code; generator/reviewer data cannot supply
evidence. Identity comparison prevents accidental stale evidence reuse, not a
malicious verifier. Evidence is retained on later workflow failures. Nothing is
fabricated when verification has not run or returns no evidence.
"""

import asyncio
from collections.abc import Awaitable
from decimal import Decimal
from typing import Literal

from pydantic import ValidationError

from .contracts import (
    AgentDecision, ModelUsage, ReviewResult, RunOutcome, TestProposal, VerificationEvidence,
)
from .lifecycle import (
    AdapterError, Analyzer, ExecutionPolicy, ExecutionReport, GenerationResponse, Generator,
    Lifecycle, ModelCallAccounting, ReasonCode, Reviewer, ReviewResponse, RunInput,
    Verifier, accounted_cost, proposal_digest,
)
from .config import AgentConfig
from .provider import ProviderError


class _Stop(Exception):
    def __init__(self, outcome: RunOutcome, code: ReasonCode, summary: str):
        self.outcome, self.code, self.summary = outcome, code, summary


class ExecutionController:
    def __init__(
        self, *, analyzer: Analyzer, generator: Generator, verifier: Verifier,
        reviewer: Reviewer | None = None,
    ) -> None:
        self.analyzer = analyzer
        self.generator = generator
        self.verifier = verifier
        self.reviewer = reviewer

    async def run_configured(self, request: RunInput, config: AgentConfig) -> ExecutionReport:
        """Trusted configuration overrides PR review preferences and run limits."""
        config = AgentConfig.model_validate(config)
        return await self.run(config.run_input(request), config.execution_policy)

    async def run(self, request: RunInput, policy: ExecutionPolicy) -> ExecutionReport:
        """Revalidate inputs; each invocation has independent counters and state.

        Component exceptions become structured failures without exposing their
        messages. Cancellation becomes an incomplete report, not success.
        """
        lifecycle = Lifecycle()
        calls = repairs = 0
        accounting: list[ModelCallAccounting] = []
        usages: list[ModelUsage] = []
        evidence_history: list[VerificationEvidence] = []
        proposal: TestProposal | None = None
        review: ReviewResult | None = None
        component = "input"
        context = {}

        def finish(outcome: RunOutcome, code: ReasonCode, summary: str) -> ExecutionReport:
            lifecycle.transition("completed" if outcome == "passed" else outcome)
            return ExecutionReport.model_validate({
                "result": {
                    "outcome": outcome, "summary": summary,
                    "verification": evidence_history[-1] if evidence_history else None,
                    "review": review, "usage": tuple(usages),
                },
                "history": lifecycle.history, "reason_code": code, "model_calls": calls,
                "repair_attempts": repairs, "proposal": proposal,
                "verification_history": tuple(evidence_history),
                "model_call_accounting": tuple(accounting),
            }, context=context)

        try:
            request = RunInput.model_validate(request)
            policy = ExecutionPolicy.model_validate(policy)
        except ValidationError:
            return finish("blocked", "invalid_input", "Invalid run input or trusted policy")

        context = {"content_limits": policy.content_limits}

        async def invoke[T](label: str, operation: Awaitable[T]) -> T:
            nonlocal component
            component = label
            return await asyncio.wait_for(operation, timeout=policy.limits.command_timeout_seconds)

        def reserve_call(label: Literal["generator", "reviewer"]) -> None:
            nonlocal calls
            if calls >= policy.limits.max_model_calls:
                raise _Stop("blocked", "model_call_limit", "Model-call limit exhausted")
            if accounted_cost(accounting) + Decimal(str(policy.estimated_call_ceiling_usd)) > Decimal(str(policy.limits.estimated_budget_usd)):
                raise _Stop("blocked", "budget_exhausted", "Estimated budget cannot cover another call")
            # Commit before invoking the component. Exceptions, cancellation,
            # timeout and invalid output leave this unknown-usage record intact.
            accounting.append(ModelCallAccounting(component=label, reserved_cost_usd=policy.estimated_call_ceiling_usd))
            calls += 1

        def record_usage(usage: ModelUsage, *, enforce_limits: bool = True) -> None:
            usages.append(usage)
            if usage.estimated_cost_usd is None:
                raise _Stop("incomplete", "unknown_cost", "Component did not report estimated cost")
            reservation = accounting[-1]
            accounting[-1] = ModelCallAccounting(
                component=reservation.component, reserved_cost_usd=reservation.reserved_cost_usd,
                reported_cost_usd=usage.estimated_cost_usd,
            )
            if enforce_limits and (
                usage.estimated_cost_usd > reservation.reserved_cost_usd
                or accounted_cost(accounting) > Decimal(str(policy.limits.estimated_budget_usd))
            ):
                raise _Stop("blocked", "budget_exhausted", "Reported estimated cost exceeded its ceiling")

        try:
            plan = AgentDecision.model_validate(
                await invoke("analyzer", self.analyzer.analyze(request, policy)), context=context,
            )
            lifecycle.transition("analyzed")
            if plan.action == "skip":
                return finish("skipped", "analysis_skipped", plan.rationale)
            if plan.action == "block":
                return finish("blocked", "analysis_blocked", plan.rationale)
            lifecycle.transition("planned")

            while True:
                reserve_call("generator")
                if proposal is not None:
                    repairs += 1
                generated = GenerationResponse.model_validate(
                    await invoke("generator", self.generator.generate(
                        request, plan, policy, previous=proposal,
                        feedback=evidence_history[-1] if evidence_history else None,
                    )), context=context,
                )
                proposal = generated.proposal
                record_usage(generated.usage)
                lifecycle.transition("generated")

                returned = await invoke("verifier", self.verifier.verify(request, proposal, policy))
                if returned is None:
                    return finish("incomplete", "missing_evidence", "Verifier returned no evidence")
                verified = VerificationEvidence.model_validate(returned)
                identity = verified.identity
                if identity is not None and (
                    identity.repository != request.pr.identity.repository
                    or identity.head_sha != request.pr.identity.head_sha
                    or identity.proposal_digest != proposal_digest(proposal)
                    or identity.policy_digest != policy.digest
                ):
                    return finish("blocked", "identity_mismatch", "Verifier evidence does not match this execution")
                evidence_history.append(verified)
                lifecycle.transition("verified")
                if verified.state != "available":
                    code = "missing_evidence" if verified.state == "missing" else "unavailable_evidence"
                    return finish("incomplete", code, "Verifier could not produce available evidence")
                if verified.outcome == "failed":
                    if repairs >= policy.limits.max_repair_attempts:
                        code = "repair_limit" if repairs else "verification_failed"
                        return finish("failed", code, "Verification failed; no repair attempts remain")
                    continue
                if verified.outcome != "passed":
                    return finish(verified.outcome, f"verification_{verified.outcome}", "Verification did not pass")
                break

            if request.review_requested:
                if self.reviewer is None:
                    return finish("blocked", "component_failure", "Review requested but no reviewer configured")
                reserve_call("reviewer")
                reviewed = ReviewResponse.model_validate(
                    await invoke("reviewer", self.reviewer.review(request, proposal, policy)), context=context,
                )
                review = reviewed.review
                record_usage(reviewed.usage)
                lifecycle.transition("reviewed")
            return finish("passed", "completed", "Execution completed with passing independent verification")
        except _Stop as stop:
            return finish(stop.outcome, stop.code, stop.summary)
        except AdapterError as exc:
            return finish("blocked", exc.code, "Adapter rejected unavailable or inconsistent input")
        except ProviderError as exc:
            if accounting and accounting[-1].reported_cost_usd is None and exc.usage is not None:
                try:
                    known_usage = ModelUsage.model_validate(exc.usage)
                except ValidationError:
                    known_usage = None
                if known_usage is not None and known_usage.estimated_cost_usd is not None:
                    # Cumulative reconciliation already happened at the provider
                    # boundary. Update only this run's receipt; do not spend again
                    # or raise another stop while handling the original failure.
                    record_usage(known_usage, enforce_limits=False)
            outcome, reason = {
                "failed": ("failed", "component_failure"),
                "timeout": ("incomplete", "timeout"),
                "cancelled": ("incomplete", "cancelled"),
                "unknown_usage": ("incomplete", "unknown_cost"),
                "budget_unknown": ("blocked", "unknown_cost"),
                "budget_exhausted": ("blocked", "budget_exhausted"),
                "authentication": ("blocked", "component_failure"),
                "rate_limit": ("incomplete", "component_failure"),
                "network_error": ("incomplete", "component_failure"),
                "service_error": ("incomplete", "component_failure"),
                "refusal": ("blocked", "component_failure"),
                "incomplete": ("incomplete", "invalid_component_output"),
                "unsupported_schema": ("blocked", "invalid_input"),
            }.get(exc.code, ("failed", "invalid_component_output"))
            return finish(outcome, reason, "Provider boundary stopped execution")
        except asyncio.CancelledError:
            return finish("incomplete", "cancelled", f"Execution cancelled during {component}")
        except TimeoutError:
            return finish("incomplete", "timeout", f"Component timed out: {component}")
        except ValidationError:
            return finish("failed", "invalid_component_output", f"Invalid component output: {component}")
        except Exception as exc:
            return finish("failed", "component_failure", f"Component failed: {component} ({type(exc).__name__})")
