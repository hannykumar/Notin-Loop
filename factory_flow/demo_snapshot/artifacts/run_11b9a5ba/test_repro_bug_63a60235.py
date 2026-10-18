from fastapi.testclient import TestClient

from app.main import app


def test_cart_total_for_user_without_cart_is_not_server_error():
    client = TestClient(app)
    response = client.get("/cart/total", params={"user": "dave"})
    assert response.status_code < 500