"""A7: investigator over sample logs, with mini_service as the replay target."""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factory import alerts, policy
from factory.contracts import RunState
from factory.stages import investigate
from factory.watcher import Watcher, iso
from test_watcher import T0, replay
from track_a_support import MINI_SERVICE

CART_SIG = "KeyError@app/cart.py:cart_total"
ORDERS_SIG = "AttributeError@app/orders.py:order_status"
STOCK_SIG = "KeyError@app/stock.py:available"


@pytest.fixture(scope="module")
def mini():
    sys.path.insert(0, str(MINI_SERVICE))
    from app.main import app
    from app.orders import order_status
    try:
        order_status("missing")
    except AttributeError:
        orders_tb = traceback.format_exc()
    yield TestClient(app, raise_server_exceptions=False), orders_tb
    sys.path.remove(str(MINI_SERVICE))
    for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        del sys.modules[name]


def sig_if_500(sig):
    return lambda resp, sample: sig if resp.status_code >= 500 else None


def breach(route, start_s, end_s, **kw):
    return {"route": route, "instance": "stable", "error_rate_percent": 100.0, "p95_latency_ms": 10.0, "samples": 5,
            "started_at": iso(T0 + timedelta(seconds=start_s)), "detected_at": iso(T0 + timedelta(seconds=end_s)), **kw}


def state_for(alert, tmp_path):
    return RunState(run_id="r1", alert=alert, artifacts_dir=tmp_path / "artifacts" / "r1")


def log_watcher(tmp_path, lines):
    log = tmp_path / "server.log"
    log.write_text("".join(json.dumps(l) + "\n" for l in lines))
    w = Watcher(log_path=log)
    w.remove_target("stable")
    w.tick()
    return w


def line(sec, path, route, status=500, method="GET", exc=None, tb=None, query=None):
    e = {"ts": iso(T0 + timedelta(seconds=sec)), "instance": "stable", "method": method, "path": path, "route": route,
         "query": query or {}, "status": status, "latency_ms": 3.0}
    if exc:
        e.update(exception_type=exc, traceback=tb)
    return e


def run(state, tmp_path, **kw):
    return investigate.run(state, releases_path=tmp_path / "releases.json", **kw)


def test_bug_cart_pass(mini, tmp_path):
    client, _ = mini
    for s, w, breaches in replay(100):
        cart = [b for b in breaches if b["route"] == "/cart/total"]
        if cart:
            break
    state = state_for(alerts.normalize(cart[0]), tmp_path)
    v = run(state, tmp_path, watcher=w, http_client=client, replay_signature=sig_if_500(CART_SIG))
    assert v.status == "pass", v.reasons
    bug = state.bug
    assert bug.signature == CART_SIG and bug.files == ["app/cart.py"] and bug.replay_failures == 3
    assert bug.exception_type == "KeyError" and bug.error_share == 1.0
    assert bug.sample_request == {"method": "GET", "path": "/cart/total", "query": {"cart_id": "empty"}, "body": None}
    assert bug.evidence_ids == ["E000", "E001", "E002", "E003", "E004"] and bug.alert_id == state.alert["incident_id"]
    assert v.evidence["gate"]["passed"] and v.evidence["gate"]["independent_source_count"] == 3   # logs, metrics, replay
    assert [a["status"] for a in v.evidence["replay_attempts"]] == [500, 500, 500]
    out = Path(v.evidence["investigation_path"])
    assert out.exists() and json.loads(out.read_text())["bug_report"]["id"] == bug.id
    assert v.evidence["investigation_sha256"] == investigate.canonical_hash(json.loads(out.read_text()))


def test_bug_404_pass_with_live_log_confirmation(mini, tmp_path):
    """No replay_signature injected: the signature is confirmed from the server's own log line."""
    client, orders_tb = mini
    lines = [line(s, "/orders/1", "/orders/{order_id}", 200) for s in range(0, 10)]
    lines += [line(s, "/orders/404", "/orders/{order_id}", 500, exc="AttributeError", tb=orders_tb) for s in range(10, 20)]
    w = log_watcher(tmp_path, lines)

    class LoggingClient:   # stands in for the live server's middleware writing server.log
        def get(self, path, params=None):
            resp = client.get(path, params=params)
            entry = line(0, path, "/orders/{order_id}", resp.status_code, exc="AttributeError", tb=orders_tb)
            entry["ts"] = iso(datetime.now(timezone.utc))
            with w.log_path.open("a") as fh:
                fh.write(json.dumps(entry) + "\n")
            return resp

    state = state_for(alerts.normalize(breach("/orders/{order_id}", 14, 20)), tmp_path)
    v = run(state, tmp_path, watcher=w, http_client=LoggingClient())
    assert v.status == "pass", v.reasons
    assert state.bug.signature == ORDERS_SIG and state.bug.files == ["app/orders.py"]
    assert state.bug.replay_failures == 3 and state.bug.sample_request["path"] == "/orders/404"
    assert "NoneType" in state.bug.traceback


def test_replay_not_confirmed_without_log_line(mini, tmp_path, monkeypatch):
    monkeypatch.setattr(investigate, "CONFIRM_WAIT_S", 0.2)
    client, orders_tb = mini
    lines = [line(s, "/orders/404", "/orders/{order_id}", 500, exc="AttributeError", tb=orders_tb) for s in range(10, 20)]
    state = state_for(alerts.normalize(breach("/orders/{order_id}", 14, 20)), tmp_path)
    v = run(state, tmp_path, watcher=log_watcher(tmp_path, lines), http_client=client)
    assert v.status == "unsure" and state.bug is None
    assert all(a["status"] == 500 and not a["failed"] for a in v.evidence["replay_attempts"])


def test_bug_stock_intermittent_unsure(tmp_path):
    w = Watcher(log_path=Path(__file__).parent / "fixtures" / "sample_server.log")
    w.remove_target("stable")
    w.tick()

    class Flaky:
        n = 0
        def get(self, path, params=None):
            self.n += 1
            return type("R", (), {"status_code": 500 if self.n == 2 else 200})()

    state = state_for(alerts.normalize(breach("/stock/{sku}/available", 110, 130)), tmp_path)
    v = run(state, tmp_path, watcher=w, http_client=Flaky(), replay_signature=sig_if_500(STOCK_SIG))
    assert v.status == "unsure" and state.bug is None
    assert any("replay" in r for r in v.reasons)
    assert v.evidence["bug_report"]["signature"] == STOCK_SIG and v.evidence["bug_report"]["replay_failures"] == 1
    assert "critical: replay reproduced only 1/3" in v.evidence["gate"]["critical_contradictions"]


@pytest.mark.parametrize("method,path,route", [("POST", "/cart/total", "/cart/total"),
                                               ("GET", "/admin/reset", "/admin/reset")])
def test_unsafe_request_not_replayed(tmp_path, method, path, route):
    tb = '  File "/srv/app/x.py", line 1, in f\nKeyError: 1\n'
    w = log_watcher(tmp_path, [line(s, path, route, method=method, exc="KeyError", tb=tb) for s in range(10, 20)])

    class NoCalls:
        def get(self, *a, **k):
            raise AssertionError("unsafe request replayed")

    state = state_for(alerts.normalize(breach(route, 14, 20)), tmp_path)
    v = run(state, tmp_path, watcher=w, http_client=NoCalls(), replay_signature=sig_if_500("x"))
    assert v.status == "unsure" and "request not safe to replay" in v.reasons and state.bug is None


def test_no_5xx_in_window_unsure(tmp_path):
    w = log_watcher(tmp_path, [line(s, "/cart/total", "/cart/total", 200) for s in range(0, 20)])
    state = state_for(alerts.normalize(breach("/cart/total", 14, 20)), tmp_path)
    v = run(state, tmp_path, watcher=w, http_client=None, replay_signature=sig_if_500(CART_SIG))
    assert v.status == "unsure" and state.bug is None


def test_mixed_signatures_unsure(mini, tmp_path):
    client, _ = mini
    tb = '  File "/srv/app/cart.py", line 5, in cart_total\nKeyError: \'items\'\n'
    lines = [line(s, "/cart/total", "/cart/total", exc="KeyError", tb=tb, query={"cart_id": "empty"}) for s in range(10, 16)]
    lines += [line(s + 0.5, "/cart/total", "/cart/total", 503) for s in range(10, 15)]
    state = state_for(alerts.normalize(breach("/cart/total", 14, 20)), tmp_path)
    v = run(state, tmp_path, watcher=log_watcher(tmp_path, lines), http_client=client,
            replay_signature=sig_if_500(CART_SIG))
    assert v.evidence["signature_counts"] == {CART_SIG: 6, "HTTP503@/cart/total": 5}
    assert v.status == "unsure" and not v.checks["dominant_share"] and state.bug is None
