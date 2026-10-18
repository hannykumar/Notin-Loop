from __future__ import annotations

from fastapi.testclient import TestClient

from app.orders import order_total


def test_root_reports_ok(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"ok": "true", "instance": "stable"}


def test_status_counts_seed_data(client: TestClient) -> None:
    payload = client.get("/status").json()

    assert payload["carts"] == 3
    assert payload["orders"] == 4
    assert payload["skus"] == 4


def test_cart_total_for_existing_cart(client: TestClient) -> None:
    response = client.get("/cart/total", params={"user": "alice"})

    assert response.status_code == 200
    assert response.json() == {"user": "alice", "total": 9.0}


def test_get_cart_lists_items(client: TestClient) -> None:
    payload = client.get("/cart/bob").json()

    assert payload["items"] == [{"sku": "bread", "qty": 2}]


def test_add_item_updates_total(client: TestClient) -> None:
    added = client.post("/cart/bob/items", json={"sku": "milk", "qty": 5})
    assert added.status_code == 200

    total = client.get("/cart/total", params={"user": "bob"}).json()["total"]
    assert total == 10.5


def test_add_item_rejects_zero_quantity(client: TestClient) -> None:
    response = client.post("/cart/bob/items", json={"sku": "milk", "qty": 0})

    assert response.status_code == 422


def test_read_existing_order(client: TestClient) -> None:
    response = client.get("/orders/o-1002")

    assert response.status_code == 200
    assert response.json() == {"id": "o-1002", "user": "bob", "total": 4.5, "status": "paid"}


def test_list_orders_for_user(client: TestClient) -> None:
    payload = client.get("/orders", params={"user": "alice"}).json()

    assert [order["id"] for order in payload["orders"]] == ["o-1001", "o-1004"]


def test_order_total_for_existing_order() -> None:
    assert order_total("o-1004") == 14.0


def test_stock_level_for_known_sku(client: TestClient) -> None:
    response = client.get("/stock/apple")

    assert response.status_code == 200
    assert response.json() == {"sku": "apple", "level": 120}


def test_stock_level_unknown_sku_is_404(client: TestClient) -> None:
    response = client.get("/stock/caviar")

    assert response.status_code == 404
