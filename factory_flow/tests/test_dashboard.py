"""Dashboard API: state from seeded runs/ files, graceful on an empty runs dir, pause/resume and break write files."""
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factory import dashboard, pipeline

FIXTURES = Path(__file__).parent / "fixtures"
TEST_KEY = "test-audit-key"   # same as gen_sample_audit.TEST_HMAC_KEY


@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("AUDIT_HMAC_KEY", TEST_KEY)
    monkeypatch.setenv("TRAFFIC_CONTROL", str(tmp_path / "traffic_control.json"))
    return tmp_path


@pytest.fixture
def client():
    return TestClient(dashboard.create_app())


def test_state_from_seeded_runs(runs, client):
    shutil.copy(FIXTURES / "sample_audit.jsonl", runs / "audit.jsonl")
    shutil.copy(FIXTURES / "sample_releases.json", runs / "releases.json")
    s = client.get("/api/state").json()
    assert s["stages"] == pipeline.STAGE_ORDER + ["rollback"]
    assert [r["run_id"] for r in s["summary"]["runs"]] == ["run_cart01", "run_stock02", "run_orders03"]
    assert [r["tag"] for r in s["releases"]] == ["release-1", "release-2"]
    assert s["chain_verified"] is True and s["paused"] is False and s["health"] is None


def test_state_empty_runs_dir_never_500(runs, client, monkeypatch):
    monkeypatch.delenv("AUDIT_HMAC_KEY")
    r = client.get("/api/state")
    assert r.status_code == 200
    s = r.json()
    assert s["summary"]["runs"] == [] and s["releases"] == [] and s["inbox"] == []
    assert s["chain_verified"] is None and s["traffic_profile"] is None
    assert client.get("/").status_code == 200


def test_pause_resume_toggle_paused_file(runs, client):
    assert client.post("/api/pause").json()["paused"] is True
    assert (runs / "PAUSED").exists() and client.get("/api/state").json()["paused"] is True
    client.post("/api/resume")
    assert not (runs / "PAUSED").exists() and client.get("/api/state").json()["paused"] is False
    # pause/resume entries have run_id ""; they must not show up as a run
    assert client.get("/api/state").json()["summary"]["runs"] == []


def test_break_writes_traffic_profile(runs, client):
    assert client.post("/api/break/cart").status_code == 200
    assert json.loads((runs / "traffic_control.json").read_text())["profile"] == "break_cart"
    assert client.get("/api/state").json()["traffic_profile"] == "break_cart"
    assert client.post("/api/break/bogus").status_code == 404


def test_report_written_and_served(runs, client):
    shutil.copy(FIXTURES / "sample_audit.jsonl", runs / "audit.jsonl")
    assert client.post("/api/report").json()["path"] == str(runs / "report.html")
    assert "SRE Dark Factory report" in client.get("/report").text
