"""BUG-CART: GET /cart/total for a user with no cart must not 500."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_cart_total_for_user_without_cart_is_zero(client: TestClient) -> None:
    response = client.get("/cart/total", params={"user": "dave"})

    assert response.status_code == 200
    assert response.json() == {"user": "dave", "total": 0}
