"""End to end on the mini repo: real triage -> investigate -> qualify -> reproduce -> fix -> inspect_diff -> oracle
through the real runner. No server is running in unit tests, so canary must stop the line and escalate:
proof that an interlock without evidence never lets a change through. (The live run is T1's run-all.)
"""
from __future__ import annotations

from factory import alerts, audit, inbox, pipeline, workspace
from factory.stages import inspect_diff
from factory.fixers import ScriptedFixer
from test_investigate import CART_SIG, mini, sig_if_500  # noqa: F401  (mini is a fixture)
from test_watcher import replay
from track_a_support import FIXTURES, MINI_SERVICE, StubWorkspace, make_mini_repo


def test_cart_bug_runs_the_real_chain_and_canary_stops_without_a_server(mini, tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("AUDIT_HMAC_KEY", "test-key")
    monkeypatch.setenv("LLM_OFFLINE", "1")          # reproduce uses the fallback test; fixer is scripted
    client, _ = mini
    for _, watcher, breaches in replay(100):
        cart = [b for b in breaches if b["route"] == "/cart/total"]
        if cart:
            break
    repo = make_mini_repo(tmp_path)
    # Track B modules hard-code factory_flow/target_service; point them at the mini repo.  # ponytail: T1 should read FACTORY_TARGET_SERVICE
    monkeypatch.setattr(workspace, "TARGET_DIR", repo)
    monkeypatch.setattr(inspect_diff, "TARGET_SERVICE_DIR", repo)
    stub_ws = StubWorkspace(repo)
    deps = {
        "watcher": watcher,
        "http_client": client,
        "replay_signature": sig_if_500(CART_SIG),
        "releases_path": tmp_path / "runs" / "releases.json",
        "prod_head": lambda: ("0" * 40, "2026-01-01T00:00:00Z"),   # old human commit: not a recent release
        "workspace": stub_ws,
        "fallback_dir": FIXTURES / "a9_fallback_tests",
        "fixer": ScriptedFixer(MINI_SERVICE / "cart_fix.diff"),
    }

    state = pipeline.run(alerts.normalize(cart[0]), deps=deps)

    statuses = {v.stage: v.status for v in state.verdicts}
    passed = ["triage", "investigate", "qualify", "reproduce", "fix", "inspect_diff", "oracle"]
    assert [v.stage for v in state.verdicts] == passed + ["canary"]
    assert all(statuses[s] == "pass" for s in passed), [(v.stage, v.status, v.reasons) for v in state.verdicts]
    assert statuses["canary"] != "pass"                              # no live server => no evidence => line stops
    assert state.bug.signature == CART_SIG and state.failing_test.exists() and state.diff
    assert state.verdicts[3].evidence["fallback_used"] is True
    assert inbox.get(state.run_id) is not None
    ok, bad = audit.verify()
    assert ok and bad is None
    assert audit.read(state.run_id)[-1]["stage"] == "run_end"
