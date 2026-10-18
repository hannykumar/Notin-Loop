"""Track A end to end: real stages triage -> investigate -> qualify -> reproduce -> fix through the real runner.

Track B stages (inspect_diff onwards) are not built yet, so the line must stop there as "unsure" and escalate:
proof that a missing interlock never lets a change through.
"""
from __future__ import annotations

from factory import alerts, audit, inbox, pipeline
from factory.fixers import ScriptedFixer
from test_investigate import CART_SIG, mini, sig_if_500  # noqa: F401  (mini is a fixture)
from test_watcher import replay
from track_a_support import FIXTURES, MINI_SERVICE, StubWorkspace, make_mini_repo


def test_cart_bug_runs_through_track_a_and_stops_at_first_missing_interlock(mini, tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("AUDIT_HMAC_KEY", "test-key")
    monkeypatch.setenv("LLM_OFFLINE", "1")          # reproduce uses the fallback test; fixer is scripted
    client, _ = mini
    for _, watcher, breaches in replay(100):
        cart = [b for b in breaches if b["route"] == "/cart/total"]
        if cart:
            break
    workspace = StubWorkspace(make_mini_repo(tmp_path))
    deps = {
        "watcher": watcher,
        "http_client": client,
        "replay_signature": sig_if_500(CART_SIG),
        "releases_path": tmp_path / "runs" / "releases.json",
        "prod_head": lambda: ("0" * 40, "2026-01-01T00:00:00Z"),   # old human commit: not a recent release
        "workspace": workspace,
        "fallback_dir": FIXTURES / "a9_fallback_tests",
        "fixer": ScriptedFixer(MINI_SERVICE / "cart_fix.diff"),
    }

    state = pipeline.run(alerts.normalize(cart[0]), deps=deps)

    statuses = {v.stage: v.status for v in state.verdicts}
    assert [v.stage for v in state.verdicts] == ["triage", "investigate", "qualify", "reproduce", "fix", "inspect_diff"]
    assert all(statuses[s] == "pass" for s in ["triage", "investigate", "qualify", "reproduce", "fix"]), state.verdicts
    assert statuses["inspect_diff"] == "unsure"                      # Track B stage missing => line stops
    assert state.bug.signature == CART_SIG and state.failing_test.exists() and state.diff
    assert state.verdicts[3].evidence["fallback_used"] is True
    assert inbox.get(state.run_id) is not None
    ok, bad = audit.verify()
    assert ok and bad is None
    assert audit.read(state.run_id)[-1]["stage"] == "run_end"
