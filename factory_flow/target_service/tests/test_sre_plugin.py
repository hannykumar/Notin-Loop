from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.sre_plugin import install


def _lines(log_file: Path) -> list[dict]:
    return [json.loads(line) for line in log_file.read_text().splitlines()]


def test_plugin_writes_one_json_line_per_request(client: TestClient, isolated_log: Path) -> None:
    client.get("/cart/total", params={"user": "alice"})
    client.get("/stock/apple")

    lines = _lines(isolated_log)
    assert len(lines) == 2
    first = lines[0]
    assert {"ts", "instance", "method", "path", "query", "status", "latency_ms"} <= set(first)
    assert first["instance"] == "stable"
    assert first["method"] == "GET"
    assert first["path"] == "/cart/total"
    assert first["query"] == {"user": "alice"}
    assert first["status"] == 200
    assert "traceback" not in first


def test_plugin_logs_route_template(client: TestClient, isolated_log: Path) -> None:
    client.get("/orders/o-1001")

    line = _lines(isolated_log)[0]
    assert line["path"] == "/orders/o-1001"
    assert line["route"] == "/orders/{order_id}"


def test_plugin_uses_instance_from_env(
    client: TestClient, isolated_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SRE_INSTANCE", "canary")

    client.get("/")

    assert _lines(isolated_log)[0]["instance"] == "canary"


def test_plugin_logs_exception_and_traceback_on_5xx(isolated_log: Path) -> None:
    demo = FastAPI()
    install(demo)

    @demo.get("/boom")
    def boom() -> None:
        raise RuntimeError("kaboom")

    response = TestClient(demo).get("/boom")

    assert response.status_code == 500
    line = _lines(isolated_log)[0]
    assert line["status"] == 500
    assert line["exception_type"] == "RuntimeError"
    assert "kaboom" in line["traceback"]
    assert "in boom" in line["traceback"]


def test_metrics_exposes_counter_and_histogram(client: TestClient, isolated_log: Path) -> None:
    client.get("/stock/apple")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert 'http_requests_total{instance="stable",method="GET",route="/stock/{sku}",status="200"}' in response.text
    assert 'http_request_duration_seconds_count{instance="stable",route="/stock/{sku}"} ' in response.text
    assert len(_lines(isolated_log)) == 1  # /metrics itself is not logged
