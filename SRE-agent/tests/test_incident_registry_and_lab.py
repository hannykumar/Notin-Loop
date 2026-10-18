from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lab.scenarios import ALERT_LAB_SCENARIOS
from integrations.incident_registry import incident_exists, incident_ids, list_mock_incidents


def test_incident_registry_lists_small_incident_path() -> None:
    ids = incident_ids()

    assert ids == ["INC-001", "INC-002", "INC-003", "INC-004", "INC-007"]
    incidents = list_mock_incidents()
    assert [item["incident_id"] for item in incidents] == ids
    ambiguous = next(item for item in incidents if item["incident_id"] == "INC-004")
    assert ambiguous["expected_incident_type"] == "Unknown"


def test_alert_lab_scenarios_reference_real_incidents() -> None:
    legacy_ids = {"INC-001", "INC-002", "INC-003"}
    kind_ids = {"KIND-OOM", "KIND-DEP-503", "KIND-DEPLOY-REGRESSION", "KIND-DNS", "KIND-AMBIGUOUS"}
    assert set(ALERT_LAB_SCENARIOS) == legacy_ids | kind_ids
    for incident_id in legacy_ids:
        assert incident_exists(incident_id)
    for scenario in ALERT_LAB_SCENARIOS.values():
        assert str(scenario.get("query", "")).strip()


def test_demo_service_exposes_core_dns_signal() -> None:
    pytest.importorskip("prometheus_client")
    from lab.demo_service import app as demo_app

    client = TestClient(demo_app)

    response = client.post("/admin/mode/dnsfailure")
    assert response.status_code == 200
    assert response.json()["mode"] == "dnsfailure"

    status = client.get("/status")
    assert status.status_code == 200
    payload = status.json()
    assert payload["signals"]["INC-003"] is True
    assert payload["signals"]["INC-001"] is False

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert 'incident_id="INC-003"' in metrics.text
    assert 'scenario="dnsfailure"' in metrics.text

    client.post("/admin/reset")


def test_demo_service_exposes_ambiguous_latency_signal() -> None:
    pytest.importorskip("prometheus_client")
    from lab.demo_service import app as demo_app

    client = TestClient(demo_app)
    response = client.post("/admin/mode/ambiguous")
    assert response.status_code == 200
    assert client.get("/").json()["mode"] == "ambiguous"
    metrics = client.get("/metrics")
    assert 'incident_id="INC-AMB"' in metrics.text
    assert 'scenario="ambiguous"' in metrics.text
    client.post("/admin/reset")


def test_dependency_mode_reports_the_same_active_signal_as_metrics() -> None:
    pytest.importorskip("prometheus_client")
    from lab.demo_service import app as demo_app

    client = TestClient(demo_app)
    client.post("/admin/mode/dependency503")

    assert client.get("/status").json()["signals"]["INC-002"] is True
    client.post("/admin/reset")


@pytest.mark.skipif(shutil.which("jq") is None, reason="Host lab script requires jq")
def test_gitops_lab_fixture_moves_between_bad_and_known_good(tmp_path: Path) -> None:
    fixture = tmp_path / "demo-api.json"
    fixture.write_text(
        json.dumps({"spec": {"template": {"spec": {"containers": [{"image": "demo-api:known-good"}]}}}}),
        encoding="utf-8",
    )
    environment = {**os.environ, "SRE_LAB_GITOPS_MANIFEST": str(fixture)}

    subprocess.run(["bash", "lab/scripts/kind_lab_gitops_version.sh", "bad-release"], check=True, env=environment)
    assert json.loads(fixture.read_text(encoding="utf-8"))["spec"]["template"]["spec"]["containers"][0]["image"] == (
        "demo-api:bad-release"
    )

    subprocess.run(["bash", "lab/scripts/kind_lab_gitops_version.sh", "known-good"], check=True, env=environment)
    assert json.loads(fixture.read_text(encoding="utf-8"))["spec"]["template"]["spec"]["containers"][0]["image"] == (
        "demo-api:known-good"
    )
