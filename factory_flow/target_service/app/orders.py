"""Orders (in memory) and the shared get_order() helper used by reports."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from fastapi import APIRouter

router = APIRouter()


@dataclass(frozen=True)
class Order:
    id: str
    user: str
    total: float
    status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ORDERS: dict[str, Order] = {
    "o-1001": Order("o-1001", "alice", 9.0, "shipped"),
    "o-1002": Order("o-1002", "bob", 4.5, "paid"),
    "o-1003": Order("o-1003", "carol", 4.6, "paid"),
    "o-1004": Order("o-1004", "alice", 14.0, "shipped"),
}


def get_order(order_id: str) -> Order | None:
    return ORDERS.get(order_id)


def order_total(order_id: str) -> float:
    order = get_order(order_id)
    return order.total if order else 0.0  # archived orders count as zero revenue


@router.get("/orders")
def list_orders(user: str) -> dict[str, Any]:
    return {"user": user, "orders": [o.to_dict() for o in ORDERS.values() if o.user == user]}


@router.get("/orders/{order_id}")
def read_order(order_id: str) -> dict[str, Any]:
    return get_order(order_id).to_dict()
