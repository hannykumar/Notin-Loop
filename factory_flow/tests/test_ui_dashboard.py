"""Dashboard API on fixture runs: cards, status stream, buddy grounding and fallback, rollback, page."""
from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factory import audit, dashboard_api, inbox, llm
from factory.contracts import BugReport, RunState, Verdict

CART_SIG = "KeyError@app/cart.py:cart_total"
STOCK_SIG = "KeyError@app/stock.py:stock_available"


class FakeWatcher:
    def __init__(self, error_rate: float = 0.0) -> None:
        self.history = {"stable": deque([(0, {"/cart/total": {}})])}
        self.scrape_errors: dict = {}
        self.error_rate = error_rate

    def tick(self) -> list:
        return []

    def current(self, path: str, instance: str = "stable") -> dict:
        return {"metrics": {"error_rate_percent": self.error_rate}, "samples": 50}


def _bug(sig: str) -> BugReport:
    return BugReport(id=f"BUG-{sig[:4]}", alert_id="A1", signature=sig, exception_type="KeyError",
                     traceback="Traceback ...", sample_request={"method": "GET", "path": "/x"}, files=["app/x.py"],
                     error_share=0.9, replay_failures=3, evidence_ids=["E1"])


def _stage(run_id: str, stage: str, status: str, reason: str, **evidence) -> None:
    audit.append({"run_id": run_id, "stage": stage, "status": status, "reasons": [reason],
                  "checks": {"ok": status == "pass"}, "evidence": evidence})


@pytest.fixture()
def runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FACTORY_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("AUDIT_HMAC_KEY", "test-key")
    dashboard_api.set_watcher(FakeWatcher())

    # demo 1: promoted and kept, with its diff in artifacts
    audit.append({"run_id": "run_cart", "stage": "run_start"})
    _stage("run_cart", "investigate", "pass", f"{CART_SIG}: share 1.0, replay 3/3",
           bug_report={"id": "BUG-CART", "signature": CART_SIG})
    for name in ("triage", "qualify", "reproduce", "fix", "inspect_diff", "oracle", "canary"):
        _stage("run_cart", name, "pass", f"{name} ok")
    _stage("run_cart", "promote", "pass", "promoted release-1", tag="release-1")
    audit.append({"run_id": "run_cart", "stage": "run_end", "outcome": "kept"})
    art = tmp_path / "artifacts" / "run_cart"
    art.mkdir(parents=True)
    (art / "fix.diff").write_text("--- a/app/cart.py\n+++ b/app/cart.py\n@@ -1 +1 @@\n-cart[\"items\"]\n+cart.get(\"items\", [])\n")

    # demo 2: refused at investigate, escalated to the inbox
    audit.append({"run_id": "run_stock", "stage": "run_start"})
    _stage("run_stock", "triage", "pass", "no own release in window")
    _stage("run_stock", "investigate", "unsure", "replay failed 1/3 (needs 3/3)",
           bug_report={"id": "BUG-STOCK", "signature": STOCK_SIG})
    state = RunState(run_id="run_stock", alert={}, artifacts_dir=tmp_path, bug=_bug(STOCK_SIG))
    inbox.escalate(state, Verdict("investigate", "unsure", ["replay failed 1/3 (needs 3/3)"], {"replay": False}, {}))
    audit.append({"run_id": "run_stock", "stage": "run_end", "outcome": "escalated"})

    (tmp_path / "releases.json").write_text(json.dumps({"releases": [{
        "tag": "release-1", "commit": "abc", "previous_tag": "release-0", "diff_sha256": "d", "run_id": "run_cart",
        "promoted_at": "2026-10-18T14:00:00Z", "watch_until": "2026-10-18T14:00:45Z", "status": "kept",
        "files_touched": ["app/cart.py"], "baseline": {}}]}))
    return tmp_path


def test_incident_cards_newest_first_with_gates_evidence_and_diff(runs: Path) -> None:
    cards = TestClient(dashboard_api.app).get("/api/incidents").json()

    assert [c["id"] for c in cards] == ["run_stock", "run_cart"]
    stock, cart = cards
    assert stock["status"] == "escalated" and stock["signature"] == STOCK_SIG
    assert stock["gate_results"]["investigate"] == "unsure" and stock["gate_results"]["oracle"] == "skipped"
    assert stock["needs_attention"] is True
    assert cart["status"] == "promoted" and cart["release_tag"] == "release-1"
    assert all(cart["gate_results"][g] == "pass" for g in ("triage", "investigate", "oracle", "canary", "promote"))
    assert cart["diff_preview"].splitlines()[-1] == '+cart.get("items", [])'
    assert "investigate: KeyError@app/cart.py:cart_total: share 1.0, replay 3/3" in cart["evidence_summary"]


def test_status_stream_reports_attention_then_active(runs: Path) -> None:
    client = TestClient(dashboard_api.app)

    def snapshot() -> dict:
        body = client.get("/api/status", params={"once": True}).text
        assert body.startswith("data: ") and body.endswith("\n\n")
        return json.loads(body[len("data: "):])

    first = snapshot()
    assert first["server_healthy"] is True
    assert first["buddy_state"] == "attention" and first["incident_count"] == 1
    assert first["current_run"] is None
    assert first["latest_release"] == {"tag": "release-1", "status": "kept"}

    audit.append({"run_id": "run_live", "stage": "run_start"})
    _stage("run_live", "triage", "pass", "no own release in window")
    second = snapshot()
    assert second["buddy_state"] == "active"
    assert second["current_run"] == "triage" and second["current_run_id"] == "run_live"
    assert second["card_status"]["run_live"] == "running"


def test_status_degraded_when_error_rate_over_alert_threshold(runs: Path) -> None:
    dashboard_api.set_watcher(FakeWatcher(error_rate=18.0))

    snapshot = dashboard_api.status_snapshot()

    assert snapshot["server_healthy"] is False
    assert "18.0%" in snapshot["health"]["reason"]


def test_buddy_is_grounded_in_the_incident_evidence(runs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_complete(system: str, user: str) -> str:
        seen.update(system=system, user=user)
        return "The agent refused because replay failed 1 of 3 times."

    monkeypatch.setattr(llm, "complete", fake_complete)
    res = TestClient(dashboard_api.app).post("/api/buddy", json={"incident_id": "run_stock", "question": "why refuse?"})

    assert res.status_code == 200
    assert res.text == "The agent refused because replay failed 1 of 3 times."
    assert "ONLY from the evidence" in seen["system"]
    assert "replay failed 1/3 (needs 3/3)" in seen["user"] and STOCK_SIG in seen["user"]
    assert "QUESTION: why refuse?" in seen["user"]
    assert "run_cart" not in seen["user"]          # other incidents are not in the context


def test_buddy_summary_and_offline_fallback(runs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def offline(system: str, user: str) -> str:
        assert dashboard_api.BUDDY_SUMMARY_QUESTION in user
        raise llm.LLMUnavailable("LLM_OFFLINE=1")

    monkeypatch.setattr(llm, "complete", offline)
    text = TestClient(dashboard_api.app).post("/api/buddy", json={"incident_id": "run_cart"}).text

    assert text.startswith("[LLM unavailable: evidence only]")
    assert CART_SIG in text and "promoted" in text


def test_buddy_unknown_incident_is_404(runs: Path) -> None:
    res = TestClient(dashboard_api.app).post("/api/buddy", json={"incident_id": "nope"})

    assert res.status_code == 404


def test_audit_trail_endpoint(runs: Path) -> None:
    entries = TestClient(dashboard_api.app).get("/api/incidents/run_stock/audit").json()

    assert [e["stage"] for e in entries] == ["run_start", "triage", "investigate", "run_end"]
    assert TestClient(dashboard_api.app).get("/api/incidents/nope/audit").status_code == 404


def test_rollback_endpoint_uses_manual_human_path(runs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from factory.stages import rollback

    calls: list = []

    def fake_rollback(tag: str, **kwargs) -> Verdict:
        calls.append((tag, kwargs))
        return Verdict("rollback", "pass", ["rolled back"], {"ok": True}, {"actor": kwargs["actor"]})

    monkeypatch.setattr(rollback, "rollback", fake_rollback)
    res = TestClient(dashboard_api.app).post("/api/rollback/release-1")

    assert res.json()["status"] == "pass"
    assert calls[0][0] == "release-1" and calls[0][1]["actor"] == "human"


def test_page_is_one_self_contained_file(runs: Path) -> None:
    res = TestClient(dashboard_api.app).get("/")

    assert res.status_code == 200
    html = res.text
    assert "new EventSource(\"/api/status\")" in html and "#0d1117" in html
    assert "<script src" not in html and "http://" not in html and "https://" not in html
    assert "innerHTML" not in html            # all evidence text goes through textContent
