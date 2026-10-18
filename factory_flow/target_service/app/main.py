# Adapted from SRE-agent/lab/demo_service.py @ b4f7e0c
# Kept: app/LOGGER structure, "/" and "/status" endpoints, prometheus_client pattern (moved to sre_plugin).
# Removed: incident failure modes and /admin endpoints; failures here come from planted bugs in the code.
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI

from app import cart, orders, reports, stock
from app.sre_plugin import install as install_sre_plugin


app = FastAPI(title="sre-dark-factory-target-service")
LOGGER = logging.getLogger("target-service")
install_sre_plugin(app)

app.include_router(cart.router)
app.include_router(orders.router)
app.include_router(reports.router)
app.include_router(stock.router)


@app.get("/")
def root() -> dict[str, str]:
    return {"ok": "true", "instance": os.getenv("SRE_INSTANCE", "stable")}


@app.get("/status")
def status() -> dict[str, Any]:
    return {
        "instance": os.getenv("SRE_INSTANCE", "stable"),
        "carts": len(cart.CARTS),
        "orders": len(orders.ORDERS),
        "skus": len(stock.LEVELS),
    }
