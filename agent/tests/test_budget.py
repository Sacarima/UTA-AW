from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from taskly_agent.budget import BudgetError, InMemoryBudgetStore
from taskly_agent.lifecycle import accounted_cost


def test_reservation_reconciliation_and_exact_decimal_boundary():
    store = InMemoryBudgetStore(0.3)
    first = store.reserve("generator", 0.2)
    store.reconcile(first, 0.1)
    second = store.reserve("reviewer", 0.2)
    assert accounted_cost(store.snapshot()) == Decimal("0.3")
    store.reconcile(second, 0.0)
    assert accounted_cost(store.snapshot()) == Decimal("0.1")
    with pytest.raises(BudgetError, match="invalid_reservation"):
        store.reconcile(first, 0.0)


def test_atomic_concurrent_reservations_cannot_share_allowance():
    store = InMemoryBudgetStore(0.05)

    def reserve(_):
        try:
            return store.reserve("generator", 0.05)
        except BudgetError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(reserve, range(20)))
    assert results.count(1) == 1
    assert results.count("exhausted") == 19
    assert accounted_cost(store.snapshot()) == Decimal("0.05")


@pytest.mark.parametrize("cap", [None, 1.0])
def test_unknown_usage_retains_reservation_and_blocks_other_executions(cap):
    store = InMemoryBudgetStore(cap)
    token = store.reserve("generator", 0.05)
    store.mark_unknown(token)
    assert store.snapshot()[0].reported_cost_usd is None
    assert accounted_cost(store.snapshot()) == Decimal("0.05")
    with pytest.raises(BudgetError, match="unknown"):
        store.reserve("reviewer", 0.01)


def test_unknown_initial_state_and_reported_overage_fail_closed():
    with pytest.raises(BudgetError, match="unknown"):
        InMemoryBudgetStore(1.0, known=False).reserve("generator", 0.05)
    store = InMemoryBudgetStore(1.0)
    token = store.reserve("generator", 0.05)
    with pytest.raises(BudgetError, match="exhausted"):
        store.reconcile(token, 0.1)
    assert accounted_cost(store.snapshot()) == Decimal("0.1")
    with pytest.raises(BudgetError, match="unknown"):
        store.reserve("generator", 0.05)


@pytest.mark.parametrize("amount", [-1.0, float("nan"), float("inf"), True, "0.05"])
def test_invalid_budget_values_are_rejected(amount):
    with pytest.raises(BudgetError):
        InMemoryBudgetStore(amount)


def test_exhausted_zero_budget_blocks_even_zero_cost_reservations():
    with pytest.raises(BudgetError, match="exhausted"):
        InMemoryBudgetStore(0.0).reserve("generator", 0.0)


@pytest.mark.parametrize("cost", [None, float("nan"), -1.0])
def test_invalid_reconciliation_marks_existing_reservation_unknown(cost):
    store = InMemoryBudgetStore(1.0)
    token = store.reserve("generator", 0.05)
    with pytest.raises(BudgetError, match="unknown"):
        store.reconcile(token, cost)
    assert accounted_cost(store.snapshot()) == Decimal("0.05")
    with pytest.raises(BudgetError, match="unknown"):
        store.reserve("generator", 0.05)


def test_reserve_returns_positive_unique_ids_and_budget_rejection_is_atomic():
    store = InMemoryBudgetStore(0.1)
    first = store.reserve("generator", 0.05)
    assert type(first) is int and first > 0
    before = store.snapshot()
    with pytest.raises(BudgetError, match="exhausted"):
        store.reserve("reviewer", 0.06)
    assert store.snapshot() == before
    second = store.reserve("reviewer", 0.05)
    assert type(second) is int and second > 0 and second != first
    assert second == first + 1
    store.reconcile(first, 0.01)
    store.reconcile(second, 0.01)
    assert accounted_cost(store.snapshot()) == Decimal("0.02")


@pytest.mark.parametrize("component,amount", [("generator", -1.0), ("generator", True), ("invalid", 0.01)])
def test_validation_rejected_reserve_creates_no_hidden_reservations(component, amount):
    store = InMemoryBudgetStore(1.0)
    with pytest.raises((BudgetError, ValueError)):
        store.reserve(component, amount)
    assert store.snapshot() == ()
    assert store.reserve("generator", 0.01) == 1


def test_unknown_budget_rejected_reserve_leaves_no_record():
    store = InMemoryBudgetStore(1.0, known=False)
    with pytest.raises(BudgetError, match="unknown"):
        store.reserve("generator", 0.01)
    assert store.snapshot() == ()
