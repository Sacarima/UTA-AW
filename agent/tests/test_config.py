import json

import pytest
from pydantic import ValidationError

from taskly_agent.config import AgentConfig, ConfigurationError, parse_config
from taskly_agent.lifecycle import ExecutionPolicy
from taskly_agent.contracts import RunLimits


@pytest.mark.parametrize("field,value", [
    ("model", ""), ("model", "injected\nmodel"), ("provider_timeout_seconds", 0),
    ("provider_timeout_seconds", float("inf")), ("provider_timeout_seconds", "30"),
    ("review_enabled", "false"), ("cumulative_budget_usd", -1.0),
    ("max_request_bytes", True), ("max_response_bytes", 0), ("api_key", "secret"),
])
def test_invalid_configuration(field, value):
    with pytest.raises(ValidationError):
        AgentConfig.model_validate({"model": "fake-model", field: value})


def test_configuration_reuses_execution_limits_and_round_trips():
    config = AgentConfig(model="fake-model", execution_policy=ExecutionPolicy(
        limits=RunLimits(max_input_tokens=100, max_output_tokens=50, max_model_calls=2,
                         estimated_budget_usd=0.2), estimated_call_ceiling_usd=0.1),
        cumulative_budget_usd=1.0, review_enabled=True)
    assert parse_config(config.model_dump_json()) == config
    assert config.execution_policy.limits.max_model_calls == 2


@pytest.mark.parametrize("payload", [
    {"model": "fake-model", "api_key": "sk-test-do-not-log"},
    {"model": "fake-model", "execution_policy": {"limits": {"max_input_tokens": "sk-test-do-not-log"}}},
])
def test_safe_configuration_errors_never_retain_secret_values(payload):
    with pytest.raises(ConfigurationError) as caught:
        parse_config(json.dumps(payload))
    assert "sk-test" not in str(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


def test_configuration_never_reads_environment_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-do-not-log")
    config = parse_config('{"model":"fake-model"}')
    assert "sk-test" not in repr(config) + config.model_dump_json()


def test_provider_configuration_requires_a_positive_reservation_ceiling():
    with pytest.raises(ValidationError):
        AgentConfig(model="fake-model", execution_policy=ExecutionPolicy(estimated_call_ceiling_usd=0.0))


@pytest.mark.parametrize("cap", [None, 0.0, -1.0, float("inf"), float("nan")])
def test_live_configuration_requires_finite_positive_cumulative_budget(cap):
    with pytest.raises(ValidationError):
        AgentConfig(model="future-model", mode="live", cumulative_budget_usd=cap)


def test_fake_mode_is_credential_free_but_not_authorized_for_live_transport():
    fake = AgentConfig(model="fake-model")
    assert fake.mode == "fake" and fake.cumulative_budget_usd is None
    with pytest.raises(ConfigurationError, match="explicit live mode"):
        fake.require_live_budget()
    live = AgentConfig(model="future-model", mode="live", cumulative_budget_usd=1.0)
    live.require_live_budget()
    assert parse_config(live.model_dump_json()) == live
    invalid = live.model_copy(update={"cumulative_budget_usd": None})
    with pytest.raises(ValidationError):
        invalid.require_live_budget()
