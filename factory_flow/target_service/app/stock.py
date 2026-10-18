"""Stock levels, plus an availability endpoint served from a refreshable cache."""
from __future__ import annotations

import random
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()

LEVELS: dict[str, int] = {"apple": 120, "bread": 30, "coffee": 12, "milk": 0}
RESERVED: dict[str, int] = {"apple": 20, "bread": 5, "coffee": 2, "milk": 0}

# Availability cache. Entries expire with jitter; about 30% of reads trigger a refresh.
REFRESH_PROBABILITY = 0.3
RELOAD_DELAY_S = 0.0005
_CACHE: dict[str, int] = {sku: LEVELS[sku] - RESERVED[sku] for sku in LEVELS}


def _cache_expired() -> bool:
    return random.random() < REFRESH_PROBABILITY


def _reload() -> None:
    time.sleep(RELOAD_DELAY_S)  # simulated warehouse round trip
    _CACHE.update({sku: LEVELS[sku] - RESERVED[sku] for sku in LEVELS})


def _refresh() -> None:
    _CACHE.clear()
    threading.Thread(target=_reload, daemon=True).start()


@router.get("/stock/{sku}")
def stock_level(sku: str) -> dict[str, Any]:
    if sku not in LEVELS:
        raise HTTPException(status_code=404, detail=f"unknown sku {sku}")
    return {"sku": sku, "level": LEVELS[sku]}


@router.get("/stock/{sku}/available")
def stock_available(sku: str) -> dict[str, Any]:
    if sku not in LEVELS:
        raise HTTPException(status_code=404, detail=f"unknown sku {sku}")
    if _cache_expired():
        _refresh()
    return {"sku": sku, "available": _CACHE[sku]}
