"""Atomic cumulative estimates, independent of controller-owned per-run limits.

Share one store across boundaries/executions. This implementation is memory
only and thread-safe within one process. A future persistent store must provide
the same atomicity across processes; a fresh store cannot recover prior spend.
These estimates are not an OpenAI account-level spending cap.
"""

import math
from decimal import Decimal
from threading import Lock
from typing import Literal, Protocol

from .lifecycle import ModelCallAccounting, accounted_cost


class BudgetError(RuntimeError):
    def __init__(self, code: Literal["exhausted", "unknown", "invalid_reservation"]):
        self.code = code
        super().__init__(f"Budget {code}")


class BudgetStore(Protocol):
    """Trusted store invariants, including for any future persistent store.

    reserve() atomically checks and commits one reservation, returning a unique
    positive int identifier (never bool). If it raises, it must leave no new
    reservation or accounting changes. The boundary cannot recover a token
    from a store that commits then raises: such a store violates this protocol.
    reconcile() finalizes a token at most once, replacing rather than adding
    cost. Overages remain charged even when reconciliation raises exhausted.
    mark_unknown() retains the ceiling and blocks new spending. These operations
    must be atomic with respect to concurrent callers.
    """

    @property
    def limit_usd(self) -> float | None: ...
    def reserve(self, component: Literal["generator", "reviewer"], amount: float) -> int: ...
    def reconcile(self, reservation: int, reported_cost: float) -> None: ...
    def mark_unknown(self, reservation: int) -> None: ...


def _money(value: float) -> Decimal:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise BudgetError("unknown")
    return Decimal(str(value))


class InMemoryBudgetStore:
    def __init__(self, limit_usd: float | None, *, known: bool = True):
        if limit_usd is not None:
            _money(limit_usd)
        if type(known) is not bool:
            raise BudgetError("unknown")
        self._limit = limit_usd
        self._known = known
        self._lock = Lock()
        self._calls: dict[int, ModelCallAccounting] = {}
        self._pending: set[int] = set()

    @property
    def limit_usd(self) -> float | None:
        return self._limit

    def snapshot(self) -> tuple[ModelCallAccounting, ...]:
        with self._lock:
            return tuple(self._calls.values())

    def reserve(self, component: Literal["generator", "reviewer"], amount: float) -> int:
        amount_decimal = _money(amount)
        call = ModelCallAccounting(component=component, reserved_cost_usd=amount)
        with self._lock:
            if not self._known:
                raise BudgetError("unknown")
            if self._limit is not None:
                spent, limit = accounted_cost(self._calls.values()), _money(self._limit)
                if spent >= limit or spent + amount_decimal > limit:
                    raise BudgetError("exhausted")
            token = len(self._calls) + 1
            self._calls[token] = call
            self._pending.add(token)
            return token

    def reconcile(self, reservation: int, reported_cost: float) -> None:
        try:
            _money(reported_cost)
        except BudgetError:
            self.mark_unknown(reservation)
            raise
        with self._lock:
            self._require_pending(reservation)
            previous = self._calls[reservation]
            self._calls[reservation] = ModelCallAccounting(
                component=previous.component, reserved_cost_usd=previous.reserved_cost_usd,
                reported_cost_usd=reported_cost,
            )
            self._pending.remove(reservation)
            if reported_cost > previous.reserved_cost_usd:
                # Record the overage, block further spending even without a cap.
                self._known = False
                raise BudgetError("exhausted")

    def mark_unknown(self, reservation: int) -> None:
        with self._lock:
            self._require_pending(reservation)
            self._pending.remove(reservation)
            self._known = False

    def _require_pending(self, reservation: int) -> None:
        if type(reservation) is not int or reservation not in self._pending:
            raise BudgetError("invalid_reservation")
