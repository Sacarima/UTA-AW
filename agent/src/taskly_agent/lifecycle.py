"""Sequential lifecycle contracts and replaceable component interfaces.

Inspired by the separation of model turns and deterministic verification in
comain/unit-test-agent, commit e25eb858135eedc2bcbf742e18f68aa16d75a30b,
uta/testgen/graph/generation-cycle.yaml. No graph engine or scheduler is used.
"""

import hashlib
import json
from collections.abc import Iterable
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .contracts import (
    AgentDecision, ContentLimits, Contract, Count, EstimatedUSD, ModelUsage,
    PRContext, ReviewResult, RunLimits, RunResult, TestProposal, VerificationEvidence,
)


ExecutionState = Literal[
    "initialized", "analyzed", "planned", "generated", "verified", "reviewed",
    "completed", "failed", "skipped", "blocked", "incomplete",
]
ReasonCode = Literal[
    "completed", "analysis_skipped", "analysis_blocked", "invalid_input",
    "invalid_component_output", "component_failure", "verification_failed",
    "missing_evidence", "unavailable_evidence", "identity_mismatch", "cancelled",
    "timeout", "model_call_limit", "repair_limit", "budget_exhausted",
    "unknown_cost", "verification_blocked", "verification_skipped", "verification_incomplete",
]


class RunInput(Contract):
    pr: PRContext
    review_requested: bool = False


def _digest(model: Contract) -> str:
    payload = json.dumps(model.model_dump(mode="json"), sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def proposal_digest(proposal: TestProposal) -> str:
    """SHA-256 of the complete proposal's canonical UTF-8 JSON, including rationale."""
    return _digest(proposal)


class ExecutionPolicy(Contract):
    """Trusted settings. Each model invocation reserves this estimated ceiling.

    Components must honor supplied token settings and report usage, without
    hidden model calls/retries. This controller cannot meter a provider itself.
    Timeouts are cooperative asyncio deadlines, not process isolation.
    """

    limits: RunLimits = Field(default_factory=RunLimits)
    content_limits: ContentLimits = Field(default_factory=ContentLimits)
    estimated_call_ceiling_usd: EstimatedUSD = 0.05

    @model_validator(mode="after")
    def at_most_one_repair(self):
        if self.limits.max_repair_attempts > 1:
            raise ValueError("this lifecycle supports at most one repair attempt")
        return self

    @property
    def digest(self) -> str:
        return _digest(self)


class GenerationResponse(Contract):
    proposal: TestProposal
    usage: ModelUsage


class ReviewResponse(Contract):
    review: ReviewResult
    usage: ModelUsage


class Analyzer(Protocol):
    """Deterministic analysis; must not make model calls."""

    async def analyze(self, request: RunInput, policy: ExecutionPolicy) -> AgentDecision: ...


class Generator(Protocol):
    """One invocation per generation/repair; returns data, never evidence."""

    async def generate(
        self, request: RunInput, plan: AgentDecision, policy: ExecutionPolicy,
        *, previous: TestProposal | None, feedback: VerificationEvidence | None,
    ) -> GenerationResponse: ...


class Reviewer(Protocol):
    async def review(
        self, request: RunInput, proposal: TestProposal, policy: ExecutionPolicy,
    ) -> ReviewResponse: ...


class Verifier(Protocol):
    """Only this trusted component produces evidence; None means none was produced."""

    async def verify(
        self, request: RunInput, proposal: TestProposal, policy: ExecutionPolicy,
    ) -> VerificationEvidence | None: ...


class ModelCallAccounting(Contract):
    """One in-memory reservation, replaced by reported cost when known.

    None means unknown usage, including interrupted/invalid responses; retain
    the full reservation. A known zero is distinct from unknown. Reported cost
    comes from ModelUsage.estimated_cost_usd, not authenticated provider billing.
    The reservation and reported cost must never be added for the same call.
    """

    component: Literal["generator", "reviewer"]
    reserved_cost_usd: EstimatedUSD
    reported_cost_usd: EstimatedUSD | None = None


def accounted_cost(calls: Iterable[ModelCallAccounting]) -> Decimal:
    """Known costs replace reservations; unknown calls retain their ceiling."""
    return sum((Decimal(str(call.reported_cost_usd if call.reported_cost_usd is not None
                            else call.reserved_cost_usd)) for call in calls), Decimal(0))


class ExecutionReport(Contract):
    result: RunResult
    history: tuple[ExecutionState, ...]
    reason_code: ReasonCode
    model_calls: Count
    repair_attempts: Count
    proposal: TestProposal | None = None
    verification_history: tuple[VerificationEvidence, ...] = ()
    model_call_accounting: tuple[ModelCallAccounting, ...] = ()


class InvalidTransition(ValueError):
    """The requested lifecycle transition is not permitted."""


class Lifecycle:
    """A per-run trace. Terminal states have no outgoing transitions."""

    _next = {
        "initialized": {"analyzed"},
        "analyzed": {"planned"},
        "planned": {"generated"},
        "generated": {"verified"},
        "verified": {"generated", "reviewed", "completed"},
        "reviewed": {"completed"},
    }
    _stops = {"failed", "skipped", "blocked", "incomplete"}

    def __init__(self) -> None:
        self._history: list[ExecutionState] = ["initialized"]

    @property
    def state(self) -> ExecutionState:
        return self._history[-1]

    @property
    def history(self) -> tuple[ExecutionState, ...]:
        return tuple(self._history)

    def transition(self, target: ExecutionState) -> None:
        if self.state not in self._next or (
            target not in self._next[self.state] and target not in self._stops
        ):
            raise InvalidTransition(f"invalid lifecycle transition: {self.state} -> {target}")
        self._history.append(target)
