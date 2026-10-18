"""Read-only recorded demo (DASHBOARD_SNAPSHOT, the Vercel deploy): reads only the snapshot, recorded health and
replays, one SSE event, 403 on every mutation, nothing written; the Vercel entry module imports from the bundle."""
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factory import dashboard_api

FIXTURES = Path(__file__).parent / "fixtures"
FACTORY_FLOW = Path(__file__).resolve().parents[1]
REPO = FACTORY_FLOW.parent
TEST_KEY = "test-audit-key"   # same as gen_sample_audit.TEST_HMAC_KEY
HEALTH = {"healthy": True, "reason": "", "worst_error_rate_percent": 0.4, "error_rate_percent": 0.4,
          "p95_latency_ms": 38.0, "alert_error_rate_percent": 5.0, "slo_p95_ms": 500}
META = {"recorded_at": "2026-10-18T15:04:05Z", "runs": 3, "note": "recorded for the test"}
REPLAYS = {"run_cart01": {"reproduced": True, "lines": ["replay run_cart01", "decision reproduced"]}}


def make_snapshot(root: Path) -> Path:
    root.mkdir(parents=True)
    shutil.copy(FIXTURES / "sample_audit.jsonl", root / "audit.jsonl")
    shutil.copy(FIXTURES / "sample_releases.json", root / "releases.json")
    for name in ("inbox", "artifacts", "deploy"):
        (root / name).mkdir()
    (root / "deploy" / "stable.json").write_text(json.dumps({"ref": "release-1", "started_ts": 0}))
    (root / "traffic_control.json").write_text(json.dumps({"profile": "normal"}))
    (root / "report.html").write_text("<h1>SRE Dark Factory report</h1>")
    for name, data in (("health.json", HEALTH), ("meta.json", META), ("replays.json", REPLAYS)):
        (root / name).write_text(json.dumps(data))
    return root


def listing(root: Path) -> dict:
    return {str(p.relative_to(root)): p.stat().st_mtime_ns for p in sorted(root.rglob("*"))}


def set_writable(root: Path, writable: bool) -> None:
    for path in [root, *root.rglob("*")]:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IWUSR if writable else mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


class NoScrapeWatcher:
    def __getattr__(self, name):
        raise AssertionError(f"snapshot mode must not touch the watcher ({name})")


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    root = make_snapshot(tmp_path / "snap")
    monkeypatch.setenv("DASHBOARD_SNAPSHOT", str(root))
    monkeypatch.setenv("FACTORY_RUNS_DIR", str(root))
    monkeypatch.setenv("AUDIT_HMAC_KEY", TEST_KEY)
    monkeypatch.setattr(dashboard_api, "_watcher", NoScrapeWatcher())
    before = listing(root)
    set_writable(root, False)          # any write attempt fails loudly
    try:
        yield root
    finally:
        set_writable(root, True)
    assert listing(root) == before     # nothing written, nothing touched


@pytest.fixture
def client(snapshot):
    return TestClient(dashboard_api.app)


def test_page_has_the_recorded_demo_banner_hook(client):
    html = client.get("/").text
    assert 'id="snapshot-banner"' in html and "Recorded demo" in html and "status.snapshot" in html


def test_status_is_recorded_one_event_and_no_watcher(client):
    body = client.get("/api/status").text     # no ?once: snapshot mode still sends one event and ends
    assert body.startswith("retry: ") and body.count("data: ") == 1
    s = json.loads(body.split("data: ", 1)[1])
    assert s["snapshot"] == {"recorded_at": META["recorded_at"], "note": META["note"]}
    assert s["health"] == HEALTH and s["server_healthy"] is True
    assert s["chain_verified"] is True and s["traffic_profile"] == "normal" and s["deployed_ref"] == "release-1"
    assert s["scoreboard"]["runs"] == 3 and [r["tag"] for r in s["releases"]] == ["release-1", "release-2"]
    assert {c["id"] for c in client.get("/api/incidents").json()} == {"run_cart01", "run_stock02", "run_orders03"}
    assert client.get("/api/incidents/run_cart01/audit").status_code == 200
    assert "SRE Dark Factory report" in client.get("/report").text


def test_chain_not_checked_without_key(client, monkeypatch):
    monkeypatch.delenv("AUDIT_HMAC_KEY")
    s = json.loads(client.get("/api/status", params={"once": True}).text.split("data: ", 1)[1])
    assert s["chain_verified"] is None


@pytest.mark.parametrize("path", ["/api/break/cart", "/api/pause", "/api/resume", "/api/rollback/release-1",
                                  "/api/report"])
def test_mutations_are_refused(client, path):
    res = client.post(path)
    assert res.status_code == 403 and res.json() == {"detail": dashboard_api.READ_ONLY_DETAIL}


def test_replay_returns_the_recorded_result(client):
    body = client.post("/api/replay/run_cart01").json()
    assert body == {"ok": True, "reproduced": True, "lines": REPLAYS["run_cart01"]["lines"]}
    assert client.post("/api/replay/run_stock02").status_code == 404


def test_buddy_still_answers_from_evidence(client, monkeypatch):
    from factory import llm

    def offline(system, user):
        raise llm.LLMUnavailable("no LLM configured")

    monkeypatch.setattr(llm, "complete", offline)
    text = client.post("/api/buddy", json={"incident_id": "run_cart01"}).text
    assert text.startswith("[LLM unavailable: evidence only]")


def test_vercel_entry_imports_from_the_bundle_alone(tmp_path):
    """Only what Vercel ships: main.py, factory_flow/factory, the snapshot, SRE-agent agent/ + runtime/.
    No factory_flow/.env, no runs/, no target_service, no inherited env."""
    bundle = tmp_path / "bundle"
    skip = shutil.ignore_patterns("__pycache__", "*.pyc")
    bundle.mkdir()
    shutil.copy(REPO / "main.py", bundle / "main.py")
    shutil.copytree(FACTORY_FLOW / "factory", bundle / "factory_flow" / "factory", ignore=skip)
    for pkg in ("agent", "runtime"):
        shutil.copytree(REPO / "SRE-agent" / pkg, bundle / "SRE-agent" / pkg, ignore=skip)
    make_snapshot(bundle / "factory_flow" / "demo_snapshot")
    script = (
        "import json, main\n"
        "from fastapi.testclient import TestClient\n"
        "c = TestClient(main.app)\n"
        "s = json.loads(c.get('/api/status').text.split('data: ', 1)[1])\n"
        "assert s['snapshot']['recorded_at'] and s['health']['healthy'] is True, s\n"
        "assert len(c.get('/api/incidents').json()) == 3\n"
        "assert c.post('/api/pause').status_code == 403\n"
        "print('ok')\n"
    )
    env = {k: v for k, v in os.environ.items()
           if k not in ("DASHBOARD_SNAPSHOT", "FACTORY_RUNS_DIR", "AUDIT_HMAC_KEY", "TRAFFIC_CONTROL",
                        "FACTORY_TARGET_SERVICE", "PYTHONPATH", "SRE_AGENT_PATH") and not k.startswith("LLM_")}
    proc = subprocess.run([sys.executable, "-c", script], cwd=bundle, env=env, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0 and proc.stdout.strip() == "ok", proc.stderr[-2000:]
    assert not (bundle / "factory_flow" / "runs").exists() and not (bundle / "factory_flow" / ".env").exists()
