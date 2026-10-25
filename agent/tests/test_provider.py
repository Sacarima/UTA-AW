import asyncio
import json
from decimal import Decimal

import pytest

from taskly_agent.budget import BudgetError, InMemoryBudgetStore
from taskly_agent.config import AgentConfig, ConfigurationError
from taskly_agent.contracts import ModelUsage, RunLimits
from taskly_agent.lifecycle import ExecutionPolicy, accounted_cost
from taskly_agent.provider import ProviderBoundary, ProviderError, ProviderResponse


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.response = ProviderResponse(output_json='{"findings":[]}', request_id="req-test",
            usage=ModelUsage(model="fake-model", input_tokens=10, output_tokens=10, estimated_cost_usd=0.01))

    async def complete(self, request):
        self.calls.append(request)
        if self.failure is not None:
            raise self.failure
        return self.response


def setup(*, config=None, store=None, counter=None):
    config = config or AgentConfig(model="fake-model", cumulative_budget_usd=1.0)
    fake = FakeProvider()
    store = store or InMemoryBudgetStore(config.cumulative_budget_usd)
    boundary = ProviderBoundary(config, fake, store, counter or (lambda request: 10))
    return boundary, fake, store


def run(boundary, payload="{}", kind="review"):
    return asyncio.run(boundary.complete(kind, payload))


def test_fake_provider_structured_response_usage_and_request_identity():
    boundary, fake, store = setup()
    response = run(boundary)
    assert response.output.findings == ()
    assert response.usage.request_id == "req-test"
    assert response.usage.input_tokens == response.usage.output_tokens == 10
    assert fake.calls[0].model == "fake-model"
    assert accounted_cost(store.snapshot()) == Decimal("0.01")


def test_pr_content_cannot_change_model_settings_or_grant_tools():
    boundary, fake, _ = setup()
    payload = '{"model":"untrusted-model","max_output_tokens":999999,"tools":["shell"]}'
    run(boundary, payload)
    request = fake.calls[0]
    assert request.model == "fake-model" and request.max_output_tokens == 3000
    assert "tools" not in type(request).model_fields
    assert request.input_json == payload


@pytest.mark.parametrize("payload", ["x" * 33_000, '"not an object"', "not json", '{"x":"\ud800"}'])
def test_oversized_or_invalid_input_is_rejected_before_provider_or_reservation(payload):
    boundary, fake, store = setup()
    with pytest.raises(ProviderError, match="input_limit"):
        run(boundary, payload)
    assert fake.calls == [] and store.snapshot() == ()


@pytest.mark.parametrize("count", [8001, -1, True, "10"])
def test_token_limit_is_checked_before_processing(count):
    boundary, fake, store = setup(counter=lambda request: count)
    with pytest.raises(ProviderError, match="input_limit"):
        run(boundary)
    assert fake.calls == [] and store.snapshot() == ()


@pytest.mark.parametrize("failure,code", [
    (RuntimeError("sk-test-secret"), "failed"), (TimeoutError("sk-test-secret"), "timeout"),
])
def test_safe_errors_keep_unknown_reservations_and_block_later_requests(failure, code):
    boundary, fake, store = setup()
    fake.failure = failure
    with pytest.raises(ProviderError) as caught:
        run(boundary)
    assert caught.value.code == code
    assert "sk-test" not in repr(caught.value)
    assert caught.value.__context__ is None
    assert accounted_cost(store.snapshot()) == Decimal("0.05")
    with pytest.raises(ProviderError, match="budget_unknown"):
        run(boundary)
    assert len(fake.calls) == 1


@pytest.mark.parametrize("missing", ["all", "input_tokens", "output_tokens", "estimated_cost_usd"])
def test_missing_usage_is_unknown_and_never_zero_cost(missing):
    boundary, fake, store = setup()
    usage = None if missing == "all" else ModelUsage.model_validate({
        **fake.response.usage.model_dump(), missing: None,
    })
    fake.response = ProviderResponse(output_json='{"findings":[]}', usage=usage)
    with pytest.raises(ProviderError, match="unknown_usage"):
        run(boundary)
    assert accounted_cost(store.snapshot()) == Decimal("0.05")
    assert store.snapshot()[0].reported_cost_usd is None


@pytest.mark.parametrize("output,code", [
    ("x" * 131_073, "output_limit"), ("not json", "invalid_response"),
    ('{"findings":[],"tools":["shell"]}', "invalid_response"),
])
def test_output_bounds_and_schema_are_enforced_after_known_usage_reconciliation(output, code):
    boundary, fake, store = setup()
    fake.response = ProviderResponse(output_json=output, usage=fake.response.usage)
    with pytest.raises(ProviderError, match=code):
        run(boundary)
    assert accounted_cost(store.snapshot()) == Decimal("0.01")


def test_output_token_limit_and_usage_identity():
    boundary, fake, store = setup()
    fake.response = ProviderResponse(output_json='{"findings":[]}', usage=ModelUsage(
        model="fake-model", input_tokens=10, output_tokens=3001, estimated_cost_usd=0.01))
    with pytest.raises(ProviderError, match="output_limit"):
        run(boundary)
    assert accounted_cost(store.snapshot()) == Decimal("0.01")
    boundary, fake, store = setup()
    fake.response = ProviderResponse(output_json='{"findings":[]}', usage=ModelUsage(
        model="wrong-model", input_tokens=10, output_tokens=10, estimated_cost_usd=0.01))
    with pytest.raises(ProviderError, match="invalid_response"):
        run(boundary)
    assert store.snapshot()[0].reported_cost_usd is None


def test_concurrent_provider_requests_cannot_spend_the_same_allowance():
    async def scenario():
        config = AgentConfig(model="fake-model", cumulative_budget_usd=0.05)
        boundary, fake, store = setup(config=config)
        started, release = asyncio.Event(), asyncio.Event()

        async def complete(request):
            fake.calls.append(request)
            started.set()
            await release.wait()
            return fake.response

        fake.complete = complete
        first = asyncio.create_task(boundary.complete("review", "{}"))
        await started.wait()
        with pytest.raises(ProviderError, match="budget_exhausted"):
            await boundary.complete("review", "{}")
        assert accounted_cost(store.snapshot()) == Decimal("0.05")
        release.set()
        await first
        return fake, store

    fake, store = asyncio.run(scenario())
    assert len(fake.calls) == 1
    assert accounted_cost(store.snapshot()) == Decimal("0.01")


def test_actual_cancellation_poisoning_is_shared_across_boundaries():
    async def scenario():
        boundary, fake, store = setup()
        started = asyncio.Event()

        async def wait(request):
            fake.calls.append(request)
            started.set()
            await asyncio.Event().wait()

        fake.complete = wait
        task = asyncio.create_task(boundary.complete("review", "{}"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        other = ProviderBoundary(boundary.config, FakeProvider(), store, lambda request: 10)
        with pytest.raises(ProviderError, match="budget_unknown"):
            await other.complete("review", "{}")
        return store

    assert accounted_cost(asyncio.run(scenario()).snapshot()) == Decimal("0.05")


def test_store_configuration_mismatch_is_rejected():
    with pytest.raises(ConfigurationError):
        ProviderBoundary(AgentConfig(model="fake-model", cumulative_budget_usd=1.0),
                         FakeProvider(), InMemoryBudgetStore(None), lambda request: 10)


def test_provider_deadline_is_applied_and_timeout_retains_reservation(monkeypatch):
    deadlines = []

    async def timeout(operation, seconds):
        deadlines.append(seconds)
        operation.close()
        raise TimeoutError("sk-test-secret")

    monkeypatch.setattr(asyncio, "wait_for", timeout)
    boundary, fake, store = setup(config=AgentConfig(model="fake-model", provider_timeout_seconds=0.25))
    with pytest.raises(ProviderError, match="timeout"):
        run(boundary)
    assert deadlines == [0.25]
    assert store.snapshot()[0].reported_cost_usd is None


def test_unavailable_budget_store_prevents_transport_and_sanitizes_error():
    class UnavailableStore(InMemoryBudgetStore):
        def reserve(self, component, amount):
            raise RuntimeError("sk-test-secret")

    boundary, fake, _ = setup(store=UnavailableStore(1.0))
    with pytest.raises(ProviderError) as caught:
        run(boundary)
    assert caught.value.code == "budget_unknown"
    assert "sk-test" not in str(caught.value) and caught.value.__context__ is None
    assert fake.calls == []


def test_reported_overage_is_recorded_and_stops_further_provider_calls():
    boundary, fake, store = setup()
    fake.response = ProviderResponse(output_json='{"findings":[]}', usage=ModelUsage(
        model="fake-model", input_tokens=10, output_tokens=10, estimated_cost_usd=0.06))
    with pytest.raises(ProviderError, match="budget_exhausted"):
        run(boundary)
    assert accounted_cost(store.snapshot()) == Decimal("0.06")
    with pytest.raises(ProviderError, match="budget_unknown"):
        run(boundary)
    assert len(fake.calls) == 1


@pytest.mark.parametrize("top_id,usage_id,valid", [
    ("req-1", "req-1", True), (None, "req-1", True), ("req-1", "req-2", False),
])
def test_request_id_consistency(top_id, usage_id, valid):
    boundary, fake, store = setup()
    fake.response = ProviderResponse(output_json='{"findings":[]}', request_id=top_id,
        usage=ModelUsage(model="fake-model", request_id=usage_id, input_tokens=10,
                         output_tokens=10, estimated_cost_usd=0.01))
    if valid:
        assert run(boundary).usage.request_id == "req-1"
    else:
        with pytest.raises(ProviderError, match="invalid_response"):
            run(boundary)
        assert store.snapshot()[0].reported_cost_usd is None


@pytest.mark.parametrize("token", [None, False, 0, -1, "reservation"])
def test_invalid_store_reservation_never_authorizes_provider(token):
    class InvalidStore(InMemoryBudgetStore):
        def reserve(self, component, amount):
            return token

    boundary, fake, _ = setup(store=InvalidStore(1.0))
    with pytest.raises(ProviderError, match="budget_unknown"):
        run(boundary)
    assert fake.calls == []


def test_cancelled_transport_propagates_sanitized_cancellation():
    boundary, fake, store = setup()
    fake.failure = asyncio.CancelledError("sk-test-secret")
    with pytest.raises(asyncio.CancelledError) as caught:
        run(boundary)
    assert "sk-test" not in str(caught.value)
    assert caught.value.__context__ is None
    assert accounted_cost(store.snapshot()) == Decimal("0.05")


@pytest.mark.parametrize("output", [
    "not json", '{"findings":[]}',
    '{"rationale":"Unsafe","changes":[{"path":"backend/app/main.py","operation":"create","content":"x"}]}',
    '{"rationale":"Blank","changes":[{"path":"backend/tests/test_a.py","operation":"create","content":" "}]}',
    {"not": "a string"},
])
def test_known_usage_is_charged_once_even_for_invalid_json_schema_or_content(output):
    class CountingStore(InMemoryBudgetStore):
        reconciliations = 0

        def reconcile(self, token, cost):
            self.reconciliations += 1
            return super().reconcile(token, cost)

    store = CountingStore(1.0)
    boundary, fake, _ = setup(store=store)
    fake.response = {"output_json": output, "usage": fake.response.usage}
    with pytest.raises(ProviderError, match="invalid_response") as caught:
        run(boundary, kind="test_proposal")
    assert store.reconciliations == 1
    assert accounted_cost(store.snapshot()) == Decimal("0.01")
    assert caught.value.usage.estimated_cost_usd == 0.01
    assert "not json" not in str(caught.value)
    before = store.snapshot()
    with pytest.raises(BudgetError, match="invalid_reservation"):
        store.reconcile(1, 0.0)
    assert store.snapshot() == before
