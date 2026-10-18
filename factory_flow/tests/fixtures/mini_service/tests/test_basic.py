from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_cart_total():
    assert client.get("/cart/total", params={"cart_id": "c1"}).json() == {"total": 5.0}


def test_order_status():
    assert client.get("/orders/1").json() == {"status": "shipped"}
