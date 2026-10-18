"""A3: runner, kill switch, inbox."""
import pytest

from factory import audit, inbox, pipeline
from factory.contracts import Verdict

ALERT = {"alert_id": "a1", "service": "target_service", "path": "/cart/total"}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("AUDIT_HMAC_KEY", "test-key")


def ok(name):
    return Verdict(stage=name, status="pass", reasons=[], checks={"ok": True}, evidence={})


def stub_stages(calls, **overrides):
    """All-pass stubs; overrides maps stage name -> fn(state)."""
    def make(name):
        def fn(state):  # STUB: replaced in step T1
            calls.append(name)
            return overrides[name](state) if name in overrides else ok(name)
        return fn
    return [(n, make(n)) for n in pipeline.STAGE_ORDER + ["rollback"]]


def stages_of(run_id):
    return [e["stage"] for e in audit.read(run_id)]


def test_full_stub_run_passes():
    calls = []
    state = pipeline.run(ALERT, stages=stub_stages(calls))
    assert calls == pipeline.STAGE_ORDER  # rollback not reached
    assert stages_of(state.run_id) == ["run_start", *pipeline.STAGE_ORDER, "run_end"]
    start, end = audit.read(state.run_id)[0], audit.read(state.run_id)[-1]
    assert start["sre_agent_commit"] and len(start["policy_sha256"]) == 64 and start["alert"] == ALERT
    assert end["outcome"] == "kept"
    assert len(state.verdicts) == 9 and state.artifacts_dir.is_dir()
    assert audit.verify() == (True, None)
    assert inbox.list_cases() == []


def test_paused_stops_before_next_stage():
    calls = []
    def pause_then_pass(state):
        pipeline.pause(reason="test")
        return ok("investigate")
    state = pipeline.run(ALERT, stages=stub_stages(calls, investigate=pause_then_pass))
    assert calls == ["triage", "investigate"]
    assert audit.read(state.run_id)[-1]["outcome"] == "paused"
    assert inbox.get(state.run_id)["stage"] == "qualify"
    pipeline.resume()
    assert not pipeline.is_paused()
    assert audit.verify() == (True, None)


def test_paused_before_run_stops_first_stage():
    pipeline.pause()
    calls = []
    state = pipeline.run(ALERT, stages=stub_stages(calls))
    assert calls == [] and inbox.get(state.run_id)["stage"] == "triage"


def test_unsure_at_qualify_stops_line():
    calls = []
    unsure = lambda s: Verdict("qualify", "unsure", ["too many files"], {"files_ok": False}, {})
    state = pipeline.run(ALERT, stages=stub_stages(calls, qualify=unsure))
    assert calls == ["triage", "investigate", "qualify"]
    case = inbox.get(state.run_id)
    assert case["status"] == "unsure" and case["reason"] == "too many files" and len(case["verdicts"]) == 3
    assert audit.read(state.run_id)[-1]["outcome"] == "escalated"


def test_triage_rollback_route_then_escalate():
    calls = []
    route = lambda s: Verdict("triage", "fail", ["own release in watch window"], {"no_own_release": False}, {}, route="rollback")
    state = pipeline.run(ALERT, stages=stub_stages(calls, triage=route))
    assert calls == ["triage", "rollback"]
    assert stages_of(state.run_id) == ["run_start", "triage", "rollback", "run_end"]
    assert audit.read(state.run_id)[-1]["outcome"] == "rolled_back"
    assert inbox.get(state.run_id)["severity"] == "normal"


def test_failed_rollback_pauses_and_escalates_critical():
    calls = []
    route = lambda s: Verdict("promote", "fail", ["SLO slipped"], {"slo": False}, {}, route="rollback")
    bad_rb = lambda s: Verdict("rollback", "fail", ["health not restored"], {"healthy": False}, {})
    state = pipeline.run(ALERT, stages=stub_stages(calls, promote=route, rollback=bad_rb))
    assert calls[-2:] == ["promote", "rollback"]
    assert inbox.get(state.run_id)["severity"] == "critical" and pipeline.is_paused()


def test_exception_becomes_unsure():
    calls = []
    def boom(state):
        raise ValueError("kaput")
    state = pipeline.run(ALERT, stages=stub_stages(calls, reproduce=boom))
    assert calls[-1] == "reproduce"
    v = state.verdicts[-1]
    assert v.status == "unsure" and "ValueError" in v.reasons[0] and "kaput" in v.reasons[0]
    assert inbox.get(state.run_id)["stage"] == "reproduce"


def test_pass_with_false_check_is_not_pass():
    calls = []
    lying = lambda s: Verdict("oracle", "pass", [], {"suite_green": False}, {})
    state = pipeline.run(ALERT, stages=stub_stages(calls, oracle=lying))
    assert state.verdicts[-1].status == "unsure" and calls[-1] == "oracle"


def test_unimplemented_stage_is_unsure(monkeypatch):
    import types, sys
    monkeypatch.setitem(sys.modules, "factory.stages.empty", types.ModuleType("factory.stages.empty"))
    for name in ("empty", "no_such_stage"):  # module without run(), and no module at all
        verdict = pipeline.resolve(name)(None)
        assert verdict.status == "unsure" and verdict.reasons == ["stage not implemented"]


def test_resolve_passes_only_declared_deps(monkeypatch):
    import types, sys
    mod = types.ModuleType("factory.stages.fake")
    mod.run = lambda state, *, workspace: ok(f"fake:{workspace}")
    monkeypatch.setitem(sys.modules, "factory.stages.fake", mod)
    v = pipeline.resolve("fake", {"workspace": "ws", "fixer": "x"})(None)
    assert v.stage == "fake:ws"
