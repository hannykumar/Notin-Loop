from fastapi.testclient import TestClient

from app.main import app


def test_cart_total_empty_cart_is_not_5xx():
    response = TestClient(app).get("/cart/total", params={"cart_id": "empty"})
    assert response.status_code < 500
