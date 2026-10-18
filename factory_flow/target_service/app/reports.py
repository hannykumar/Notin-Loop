"""Daily revenue report. Rarely called; uses the shared get_order() helper via order_total()."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.orders import order_total

router = APIRouter()

# Order ids booked today. "o-0998" has since been archived (get_order returns None).
DAILY_LEDGER: list[str] = ["o-0998", "o-1001", "o-1002", "o-1003", "o-1004"]


@router.get("/reports/daily")
def daily_report() -> dict[str, Any]:
    revenue = sum(order_total(order_id) for order_id in DAILY_LEDGER)
    return {"orders": len(DAILY_LEDGER), "revenue": round(revenue, 2)}
