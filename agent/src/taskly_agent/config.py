"""Trusted, explicit configuration. No environment access or credential fields.

Use parse_config at an external boundary for safe validation errors. Pass this
configuration independently from PR metadata; never merge the two mappings.
"""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, ValidationError, model_validator

from .contracts import Contract, EstimatedUSD
from .lifecycle import ExecutionPolicy, RunInput


class ConfigurationError(ValueError):
    """Configuration rejected; contains no input values."""


class AgentConfig(Contract):
    model_config = ConfigDict(hide_input_in_errors=True)

    model: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
    mode: Literal["fake", "live"] = "fake"
    provider_timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 30.0
    execution_policy: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    cumulative_budget_usd: EstimatedUSD | None = None
    review_enabled: bool = False
    max_request_bytes: Annotated[int, Field(gt=0)] = 32_768
    max_response_bytes: Annotated[int, Field(gt=0)] = 131_072

    @model_validator(mode="after")
    def positive_provider_reservation(self):
        if self.execution_policy.estimated_call_ceiling_usd <= 0:
            raise ValueError("provider calls require a positive reservation ceiling")
        if self.mode == "live" and (self.cumulative_budget_usd is None or self.cumulative_budget_usd <= 0):
            raise ValueError("live execution requires a finite positive cumulative budget")
        return self

    def require_live_budget(self) -> None:
        """A future live transport must call this before initializing its client.

        Fake mode is convenient configuration, not a network sandbox. No live
        transport exists here; its future opt-in must not rely on the default.
        """
        config = AgentConfig.model_validate(self)
        if config.mode != "live":
            raise ConfigurationError("Live execution requires explicit live mode")

    def run_input(self, request: RunInput) -> RunInput:
        """Only trusted configuration decides whether configured runs review."""
        request = RunInput.model_validate(request)
        return RunInput(pr=request.pr, review_requested=self.review_enabled)


def parse_config(raw: str) -> AgentConfig:
    """Reject unknown fields (including API keys) without echoing their values."""
    try:
        return AgentConfig.model_validate_json(raw)
    except (ValidationError, ValueError, TypeError):
        pass
    # Raise outside the handler so the original input-bearing exception is not
    # attached as exception context. No logger or environment lookup exists.
    raise ConfigurationError("Invalid agent configuration")
