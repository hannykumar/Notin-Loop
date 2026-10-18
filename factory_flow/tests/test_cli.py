"""T1: cli argument parsing, fixer routing, queue consumer, scoreboard (fakes only, no servers)."""
import copy
import json
import queue
import threading

import pytest

from factory import cli, llm
from factory.contracts import BugReport


def bug(files):
    return BugReport(id="BUG-1", alert_id="A-1", signature="X@" + files[0], exception_type="KeyError", traceback="",
                     sample_request={"method": "GET", "path": "/x"}, files=files, error_share=1.0,
                     replay_failures=3, evidence_ids=[])


# ---------------------------------------------------------------- parsing

def test_parse_commands():
    p = cli.build_parser()
    a = p.parse_args(["start", "--fixer", "scripted", "--no-traffic"])
    assert (a.fn, a.fixer, a.no_traffic) == (cli.cmd_start, "scripted", True)
    assert p.parse_args(["start"]).fixer == "llm"
    assert p.parse_args(["break", "404"]).target == "404"
    assert p.parse_args(["rollback", "release-2"]).tag == "release-2"
    assert p.parse_args(["replay", "run_ab12"]).run_id == "run_ab12"
    assert p.parse_args(["run-all", "--fixer", "scripted"]).fn == cli.cmd_run_all
    for name in ("inbox", "pause", "resume", "report"):
        assert p.parse_args([name]).fn is getattr(cli, f"cmd_{name}")
    with pytest.raises(SystemExit):
        p.parse_args(["break", "everything"])
    assert cli.PROFILES["404"] == "break_404"


# ---------------------------------------------------------------- RoutingFixer

@pytest.fixture
def patches(tmp_path):
    (tmp_path / "orders_helper_fix.diff").write_text("ORDERS")
    (tmp_path / "cart_fix.diff").write_text("CART")
    return tmp_path


def test_orders_bug_always_uses_scripted_orders_patch(patches):
    called = []
    f = cli.RoutingFixer("llm", patches_dir=patches, llm_ready=lambda: True, llm_fixer=lambda: called.append(1))
    assert f.fix(None, bug(["app/orders.py"]), None, 5) == "ORDERS"
    assert f.name == "scripted" and not called


def test_cart_bug_scripted_when_llm_offline(patches):
    f = cli.RoutingFixer("llm", patches_dir=patches, llm_ready=lambda: False)
    assert f.fix(None, bug(["app/cart.py"]), None, 5) == "CART" and f.name == "scripted"


def test_cart_bug_llm_then_fallback_when_unavailable(patches):
    class Down:
        name = "llm"

        def fix(self, *a):
            raise llm.LLMUnavailable("down")

    class Up(Down):
        def fix(self, *a):
            return "LLMDIFF"

    up = cli.RoutingFixer("llm", patches_dir=patches, llm_ready=lambda: True, llm_fixer=Up)
    assert up.fix(None, bug(["app/cart.py"]), None, 5) == "LLMDIFF" and up.name == "llm"
    down = cli.RoutingFixer("llm", patches_dir=patches, llm_ready=lambda: True, llm_fixer=Down)
    assert down.fix(None, bug(["app/cart.py"]), None, 5) == "CART" and down.name == "scripted"


def test_no_fixer_for_other_bug_without_llm(patches):
    f = cli.RoutingFixer("scripted", patches_dir=patches, llm_ready=lambda: True)
    with pytest.raises(ValueError):
        f.fix(None, bug(["app/stock.py"]), None, 5)


def test_llm_ready_respects_offline(monkeypatch):
    monkeypatch.setenv("LLM_OFFLINE", "1")
    assert cli.llm_ready() is False


# ---------------------------------------------------------------- consumer

def test_consume_runs_pipeline_then_done():
    calls, stop, q = [], threading.Event(), queue.Queue()
    q.put({"fingerprint": "fp1"})

    class Intake:
        def done(self, fp):
            calls.append(("done", fp))
            stop.set()

    class Client:
        def close(self):
            calls.append(("close",))

    def run(alert, deps):
        calls.append(("run", alert["fingerprint"], deps["marker"]))
        return "STATE"

    results = []
    cli.consume(q, Intake(), lambda: {"marker": 7, "http_client": Client()}, stop, run=run,
                after_run=lambda s: calls.append(("after", s)), results=results)
    assert calls == [("run", "fp1", 7), ("after", "STATE"), ("close",), ("done", "fp1")]
    assert results == ["STATE"]


def test_consume_calls_done_even_if_run_crashes():
    calls, stop, q = [], threading.Event(), queue.Queue()
    q.put({"fingerprint": "fp2"})

    class Intake:
        def done(self, fp):
            calls.append(fp)
            stop.set()

    def run(alert, deps):
        raise RuntimeError("boom")

    cli.consume(q, Intake(), dict, stop, run=run)
    assert calls == ["fp2"]


# ---------------------------------------------------------------- scoreboard

SCENARIOS = json.loads(cli.SCENARIOS_PATH.read_text())


def fabricated():
    def start(run_id, path):
        return {"run_id": run_id, "stage": "run_start", "alert": {"labels": {"path": path}}}

    def end(run_id, outcome):
        return {"run_id": run_id, "stage": "run_end", "outcome": outcome}

    entries = [start("r1", "/cart/total"), end("r1", "kept"),
               start("r2", "/stock/{sku}/available"), end("r2", "escalated"),
               start("r2b", "/stock/{sku}/available"), end("r2b", "paused"),   # re-fire: first run counts
               start("r3", "/orders/{order_id}"), end("r3", "rolled_back")]
    ledger = [{"tag": "release-1", "run_id": "r1", "commit": "c1", "status": "kept"},
              {"tag": "release-2", "run_id": "r3", "commit": "c2", "status": "rolled_back"}]
    cases = [{"run_id": "r2", "stage": "investigate"}, {"run_id": "r3", "stage": "watch"},
             {"run_id": "r3", "kind": "rollback"}]
    final = {"prod_head": "c1", "prod_release": "release-1", "stable_ref": "release-1", "stable_healthy": True,
             "line_paused": True}
    return entries, ledger, cases, final


def test_scoreboard_all_match():
    rows, bad, ok = cli.score(SCENARIOS, *fabricated())
    assert ok and bad == 0, rows
    assert [r["run_id"] for r in rows] == ["r1", "r2", "r3", None]


def test_scoreboard_mismatches():
    entries, ledger, cases, final = fabricated()
    entries[-1] = {"run_id": "r3", "stage": "run_end", "outcome": "kept"}       # demo 3 never rolled back
    ledger[1]["status"] = "watching"
    final.update(prod_head="c2", prod_release="release-2", stable_ref="release-2", line_paused=False)
    rows, bad, ok = cli.score(SCENARIOS, entries, ledger, cases, final)
    assert not ok
    assert {r["id"] for r in rows if not r["match"]} == {"demo3", "final"}
    assert bad == 2   # release-2 live on prod without a clean verdict + prod not on release-1


def test_scoreboard_wrong_escalation_stage_and_missing_run():
    entries, ledger, cases, final = fabricated()
    cases[0]["stage"] = "qualify"
    entries = [e for e in entries if e["run_id"] != "r1"]
    rows, bad, ok = cli.score(SCENARIOS, entries, ledger, cases, final)
    assert not ok and bad == 0
    by_id = {r["id"]: r for r in rows}
    assert by_id["demo1"]["actual"]["outcome"] == "no run" and not by_id["demo2"]["match"]


def test_scenarios_file_shape():
    s = copy.deepcopy(SCENARIOS)
    assert [x["break"] for x in s["scenarios"]] == ["cart", "stock", "404"]
    assert [x["expected"]["outcome"] for x in s["scenarios"]] == ["kept", "escalated", "rolled_back"]
    assert s["final_state"]["prod_release"] == "release-1" and s["final_state"]["bad_changes_left"] == 0
