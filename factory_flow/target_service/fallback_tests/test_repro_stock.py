"""BUG-STOCK: GET /stock/{sku}/available fails while the availability cache refreshes."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import stock


def test_available_survives_cache_refresh(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stock, "_cache_expired", lambda: True)

    response = client.get("/stock/apple/available")

    assert response.status_code == 200
    assert response.json() == {"sku": "apple", "available": 100}
