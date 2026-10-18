"""BUG-404: GET /orders/{id} for a missing order must return 404, not 500. Self-contained (A9 lint)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_missing_order_is_404() -> None:
    response = TestClient(app).get("/orders/o-9999")

    assert response.status_code == 404
