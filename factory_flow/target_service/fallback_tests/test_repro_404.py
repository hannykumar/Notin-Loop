"""BUG-404: GET /orders/{id} for a missing order must return 404, not 500."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_missing_order_is_404(client: TestClient) -> None:
    response = client.get("/orders/o-9999")

    assert response.status_code == 404
