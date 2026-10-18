from fastapi.testclient import TestClient

from app.main import app


def test_read_missing_order_does_not_return_server_error(monkeypatch):
    monkeypatch.setattr("app.sre_plugin._write_line", lambda entry: None)
    client = TestClient(app)
    response = client.get("/orders/o-9999", params={})
    assert response.status_code < 500