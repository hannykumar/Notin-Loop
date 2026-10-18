"""BUG-CART: GET /cart/total for a user with no cart must not 500. Self-contained (A9 lint)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_cart_total_for_user_without_cart_is_zero() -> None:
    response = TestClient(app).get("/cart/total", params={"user": "dave"})

    assert response.status_code == 200
    assert response.json() == {"user": "dave", "total": 0}
