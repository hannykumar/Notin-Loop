"""Shopping carts (in memory)."""
from __future__ import annotations

import copy
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter()

PRICES: dict[str, float] = {"apple": 0.5, "bread": 2.25, "coffee": 7.0, "milk": 1.2}
SEED_CARTS: dict[str, dict[str, Any]] = {
    "alice": {"items": [{"sku": "apple", "qty": 4}, {"sku": "coffee", "qty": 1}]},
    "bob": {"items": [{"sku": "bread", "qty": 2}]},
    "carol": {"items": [{"sku": "milk", "qty": 3}, {"sku": "apple", "qty": 2}]},
}
CARTS: dict[str, dict[str, Any]] = copy.deepcopy(SEED_CARTS)


class CartItem(BaseModel):
    sku: str
    qty: int = Field(gt=0)


def reset() -> None:
    CARTS.clear()
    CARTS.update(copy.deepcopy(SEED_CARTS))


@router.get("/cart/total")
def cart_total(user: str) -> dict[str, Any]:
    cart = CARTS.get(user, {})
    total = sum(PRICES.get(item["sku"], 0.0) * item["qty"] for item in cart["items"])
    return {"user": user, "total": round(total, 2)}


@router.get("/cart/{user}")
def get_cart(user: str) -> dict[str, Any]:
    return {"user": user, "items": CARTS.get(user, {}).get("items", [])}


@router.post("/cart/{user}/items")
def add_item(user: str, item: CartItem) -> dict[str, Any]:
    cart = CARTS.setdefault(user, {"items": []})
    cart["items"].append(item.model_dump())
    return {"user": user, "items": cart["items"]}
