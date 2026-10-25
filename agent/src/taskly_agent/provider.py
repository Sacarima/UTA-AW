"""Bounded structured-provider boundary; no HTTP, SDK, credentials or tools.

The controller owns per-run limits and call accounting. This boundary reserves
only the independent cumulative allowance, once per transport invocation.
Injected transports must not retry secretly and must limit response bytes while
reading them; the post-return check here cannot bound their peak allocation.
The injected token counter must include the eventual provider framing/schema.
"""

import asyncio
import json
from collections.abc import Callable
from typing import Annotated, Literal, Protocol

from pydantic import Field

from .budget import BudgetError, BudgetStore
from .config import AgentConfig, ConfigurationError
from .contracts import Contract, ModelUsage, ReviewResult, TestProposal


ResponseKind = Literal["test_proposal", "review"]
ProviderErrorCode = Literal[
    "failed", "timeout", "cancelled", "input_limit", "output_limit",
    "invalid_response", "unknown_usage", "budget_exhausted", "budget_unknown",
]


class ProviderError(RuntimeError):
    """Only a fixed code is exposed; raw transport errors are never retained."""

    def __init__(self, code: ProviderErrorCode, *, usage: ModelUsage | None = None):
        allowed = {"failed", "timeout", "cancelled", "input_limit", "output_limit",
                   "invalid_response", "unknown_usage", "budget_exhausted", "budget_unknown"}
        self.code = code if code in allowed else "failed"
        # Validated usage may accompany an unusable output. It is data for the
        # controller's separate per-run ledger, never part of the error message.
        self.usage = usage
        super().__init__(f"Provider {self.code}")


class ProviderRequest(Contract):
    model: str
    response_kind: ResponseKind
    input_json: str
    max_input_tokens: Annotated[int, Field(gt=0)]
    max_output_tokens: Annotated[int, Field(gt=0)]


class ProviderResponse(Contract):
    output_json: str
    usage: ModelUsage | None
    request_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")] | None = None


class StructuredResponse(Contract):
    output: TestProposal | ReviewResult
    usage: ModelUsage


class Provider(Protocol):
    async def complete(self, request: ProviderRequest) -> ProviderResponse: ...


class ProviderBoundary:
    def __init__(self, config: AgentConfig, transport: Provider, store: BudgetStore,
                 count_input_tokens: Callable[[ProviderRequest], int]) -> None:
        self.config = AgentConfig.model_validate(config)
        if store.limit_usd != self.config.cumulative_budget_usd:
            raise ConfigurationError("Cumulative store limit must match trusted configuration")
        self._transport, self._store, self._count_tokens = transport, store, count_input_tokens

    async def complete(self, response_kind: ResponseKind, input_json: str) -> StructuredResponse:
        """Model/settings/schema are selected by trusted code, never PR fields."""
        request = None
        try:
            if type(input_json) is not str or len(input_json.encode("utf-8")) > self.config.max_request_bytes:
                raise ValueError
            if not isinstance(json.loads(input_json), dict):
                raise ValueError
            limits = self.config.execution_policy.limits
            request = ProviderRequest(model=self.config.model, response_kind=response_kind,
                                      input_json=input_json, max_input_tokens=limits.max_input_tokens,
                                      max_output_tokens=limits.max_output_tokens)
            count = self._count_tokens(request)
            if type(count) is not int or count < 0 or count > limits.max_input_tokens:
                request = None
        except Exception:
            request = None
        if request is None:
            raise ProviderError("input_limit")

        reservation = None
        failure = None
        try:
            reservation = self._store.reserve(
                "generator" if response_kind == "test_proposal" else "reviewer",
                self.config.execution_policy.estimated_call_ceiling_usd,
            )
            if type(reservation) is not int or reservation <= 0:
                failure = "budget_unknown"
        except BudgetError as exc:
            failure = "budget_exhausted" if exc.code == "exhausted" else "budget_unknown"
        except Exception:
            failure = "budget_unknown"
        if failure:
            raise ProviderError(failure)

        response = None
        cancelled = False
        try:
            raw = await asyncio.wait_for(self._transport.complete(request), self.config.provider_timeout_seconds)
            # Validate usage/identity independently of output content. Even a
            # malformed output envelope must not erase otherwise reliable cost.
            metadata = raw.model_dump() if isinstance(raw, ProviderResponse) else raw
            response = ProviderResponse.model_validate({
                "output_json": "", "usage": metadata.get("usage"),
                "request_id": metadata.get("request_id"),
            })
        except asyncio.CancelledError:
            cancelled = True
            failure = "cancelled"
        except TimeoutError:
            failure = "timeout"
        except Exception:
            failure = "failed"

        if response is not None:
            usage = response.usage
            if usage is None or usage.input_tokens is None or usage.output_tokens is None or usage.estimated_cost_usd is None:
                failure = "unknown_usage"
            elif usage.model != self.config.model or (
                usage.request_id is not None and response.request_id is not None
                and usage.request_id != response.request_id
            ):
                failure = "invalid_response"
        if failure:
            try:
                self._store.mark_unknown(reservation)
            except Exception:
                failure = "budget_unknown"
            if cancelled:
                # Preserve task cancellation even if accounting cleanup fails.
                # Do not retain potentially sensitive transport exception text.
                raise asyncio.CancelledError() from None
            raise ProviderError(failure)

        known_usage = ModelUsage.model_validate({
            **response.usage.model_dump(),
            "request_id": response.request_id or response.usage.request_id,
        })
        try:
            self._store.reconcile(reservation, known_usage.estimated_cost_usd)
        except BudgetError as exc:
            failure = "budget_exhausted" if exc.code == "exhausted" else "budget_unknown"
        except Exception:
            failure = "budget_unknown"
        if failure:
            raise ProviderError(failure, usage=known_usage)

        result = None
        try:
            response = ProviderResponse.model_validate(raw)
            if (
                len(response.output_json.encode("utf-8")) > self.config.max_response_bytes
                or response.usage.output_tokens > limits.max_output_tokens
                or response.usage.input_tokens > limits.max_input_tokens
            ):
                failure = "output_limit"
            else:
                schema = TestProposal if response_kind == "test_proposal" else ReviewResult
                context = {"content_limits": self.config.execution_policy.content_limits}
                output = schema.model_validate_json(response.output_json, context=context)
                result = StructuredResponse.model_validate({"output": output, "usage": known_usage}, context=context)
        except Exception:
            failure = "invalid_response"
        if failure:
            raise ProviderError(failure, usage=known_usage)
        return result
