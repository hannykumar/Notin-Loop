"""Combined dashboard, control side: status fields from seeded runs/ files, graceful on an empty runs dir,
pause/resume, break, replay, report, and the CLI's --ui hook serving the same app."""
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factory import dashboard_api, pipeline

FIXTURES = Path(__file__).parent / "fixtures"
TEST_KEY = "test-audit-key"   # same as gen_sample_audit.TEST_HMAC_KEY


class QuietWatcher:
    """A fresh watcher with traffic: current() must be the only call (no forced tick)."""
    history = {"stable": [(0, {"/cart/total": {}})]}
    scrape_errors: dict = {}

    def tick(self):
        raise AssertionError("server_health must not force a tick on a shared watcher")

    def current(self, path, instance="stable"):
        return {"metrics": {"error_rate_percent": 1.5, "p95_latency_ms": 42.0}, "samples": 20}


@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("AUDIT_HMAC_KEY", TEST_KEY)
    monkeypatch.setenv("TRAFFIC_CONTROL", str(tmp_path / "traffic_control.json"))
    monkeypatch.setattr(dashboard_api, "_watcher", QuietWatcher())
    return tmp_path


@pytest.fixture
def client():
    return TestClient(dashboard_api.app)


def status(client) -> dict:
    return json.loads(client.get("/api/status", params={"once": True}).text[len("data: "):])


def test_status_from_seeded_runs(runs, client):
    shutil.copy(FIXTURES / "sample_audit.jsonl", runs / "audit.jsonl")
    shutil.copy(FIXTURES / "sample_releases.json", runs / "releases.json")
    s = status(client)
    assert [r["tag"] for r in s["releases"]] == ["release-1", "release-2"]
    assert s["chain_verified"] is True and s["paused"] is False
    assert s["health"]["error_rate_percent"] == 1.5 and s["health"]["p95_latency_ms"] == 42.0
    assert s["scoreboard"]["runs"] == 3
    cards = client.get("/api/incidents").json()
    assert {c["id"] for c in cards} == {"run_cart01", "run_stock02", "run_orders03"}
    orders = next(c for c in cards if c["id"] == "run_orders03")
    assert orders["gate_results"]["watch"] == "fail" and orders["gate_results"]["rollback"] == "pass"
    assert "AttributeError@app/orders.py:daily_report" in orders["gate_reasons"]["watch"]


def test_status_empty_runs_dir_never_breaks(runs, client, monkeypatch):
    monkeypatch.delenv("AUDIT_HMAC_KEY")
    monkeypatch.setattr(dashboard_api, "_watcher", None)   # health itself blows up -> nulls, not a broken stream
    s = status(client)
    assert s["releases"] == [] and s["card_status"] == {} and s["server_healthy"] is None
    assert s["chain_verified"] is None and s["traffic_profile"] is None
    assert client.get("/api/incidents").json() == []
    assert client.get("/").status_code == 200


def test_pause_resume_toggle_paused_file(runs, client):
    assert client.post("/api/pause").json()["paused"] is True
    assert (runs / "PAUSED").exists() and status(client)["paused"] is True
    client.post("/api/resume")
    assert not (runs / "PAUSED").exists() and status(client)["paused"] is False
    assert [e["actor"] for e in pipeline.audit.read() if e["stage"] in ("pause", "resume")] == ["human", "human"]
    # pause/resume entries have run_id ""; they must not show up as an incident
    assert client.get("/api/incidents").json() == []


def test_break_writes_traffic_profile(runs, client):
    assert client.post("/api/break/cart").status_code == 200
    assert json.loads((runs / "traffic_control.json").read_text())["profile"] == "break_cart"
    assert status(client)["traffic_profile"] == "break_cart"
    assert client.post("/api/break/bogus").status_code == 404


def test_replay_endpoint(runs, client, monkeypatch):
    from factory import replay

    monkeypatch.setattr(replay, "replay", lambda run_id: {"run_id": run_id, "chain_ok": True, "first_bad_seq": None,
                                                           "checks": [], "reproduced": True, "reasons": []})
    body = client.post("/api/replay/run_cart01").json()
    assert body["reproduced"] is True and body["lines"][-1] == "decision reproduced"


def test_report_written_and_served(runs, client):
    shutil.copy(FIXTURES / "sample_audit.jsonl", runs / "audit.jsonl")
    assert client.post("/api/report").json()["path"] == str(runs / "report.html")
    assert "SRE Dark Factory report" in client.get("/report").text


def test_serve_in_thread_uses_the_agents_watcher(monkeypatch):
    import uvicorn

    served = {}
    monkeypatch.setattr(uvicorn.Server, "run", lambda self: served.update(app=self.config.app, port=self.config.port))
    monkeypatch.setattr(dashboard_api, "_watcher", None)
    agent = type("Agent", (), {"watcher": QuietWatcher()})()
    dashboard_api.serve_in_thread(agent, 8099).join(timeout=5)
    assert served == {"app": dashboard_api.app, "port": 8099} and dashboard_api._watcher is agent.watcher
