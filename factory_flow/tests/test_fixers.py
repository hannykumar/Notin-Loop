"""A10: fixers return only a diff; run_fix passes only for a non-empty diff that applies to prod."""
import subprocess
import time
from pathlib import Path

import pytest

from factory import fixers, llm
from factory.contracts import BugReport, RunState
from tests.track_a_support import MINI_SERVICE, StubWorkspace, make_mini_repo

CART_FIX = MINI_SERVICE / "cart_fix.diff"
REPRO_TEST = """from fastapi.testclient import TestClient
from app.main import app


def test_empty_cart_total():
    assert TestClient(app).get("/cart/total", params={"cart_id": "empty"}).json() == {"total": 0}
"""
BUG = BugReport(
    id="BUG-1", alert_id="A1", signature="KeyError@app/cart.py:cart_total", exception_type="KeyError",
    traceback="KeyError: 'items'", sample_request={"method": "GET", "path": "/cart/total", "query": {"cart_id": "empty"}},
    files=["app/cart.py"], error_share=0.9, replay_failures=3, evidence_ids=["E000"],
)


@pytest.fixture
def setup(tmp_path):
    repo = make_mini_repo(tmp_path)
    test = tmp_path / "test_repro_cart.py"
    test.write_text(REPRO_TEST)
    state = RunState(run_id="r1", alert={}, artifacts_dir=tmp_path, bug=BUG, failing_test=test)
    workspace = StubWorkspace(repo)
    yield state, workspace, tmp_path
    workspace.cleanup()


def test_scripted_fix_applies_and_passes(setup):
    state, workspace, _ = setup
    tree = workspace.fresh_worktree("prod")
    subprocess.run(["git", "apply", "--check", str(CART_FIX)], cwd=tree, check=True)
    workspace.cleanup()

    verdict = fixers.run_fix(state, fixers.ScriptedFixer(CART_FIX), workspace=workspace)
    assert verdict.status == "pass" and all(verdict.checks.values()), verdict.reasons
    assert state.diff == CART_FIX.read_text()
    assert verdict.evidence["fixer"] == "scripted" and len(verdict.evidence["diff_sha256"]) == 64
    assert workspace._trees == []  # throwaway worktrees removed


@pytest.mark.parametrize("content", ["", "this is not a diff\n", "--- a/app/nope.py\n+++ b/app/nope.py\n@@ -1 +1 @@\n-x\n+y\n"])
def test_fixture_fixer_bad_diff_is_not_pass(setup, content):
    state, workspace, tmp_path = setup
    bad = tmp_path / "bad.diff"
    bad.write_text(content)
    verdict = fixers.run_fix(state, fixers.FixtureFixer(bad), workspace=workspace)
    assert verdict.status != "pass" and state.diff == ""
    assert verdict.evidence["fixer"] == "fixture"


def test_llm_fixer_fenced_diff(setup, monkeypatch):
    state, workspace, _ = setup
    prompts = []
    reply = f"```diff\n{CART_FIX.read_text()}```"
    monkeypatch.setattr(llm, "complete", lambda system, user: prompts.append(user) or llm.strip_fences(reply))
    verdict = fixers.run_fix(state, fixers.LLMFixer(), workspace=workspace)
    assert verdict.status == "pass", verdict.reasons
    assert "cart_total" in prompts[0] and "test_empty_cart_total" in prompts[0] and 'cart["items"]' in prompts[0]
    assert "llm" in verdict.evidence


def test_llm_fixer_garbage_twice(setup, monkeypatch, tmp_path):
    state, workspace, _ = setup
    calls = []
    monkeypatch.setattr(llm, "complete", lambda s, u: calls.append(1) or "I think you should add a guard.")
    with pytest.raises(ValueError):
        fixers.LLMFixer().fix(tmp_path, BUG, state.failing_test, 10)
    assert len(calls) == 2
    verdict = fixers.run_fix(state, fixers.LLMFixer(), workspace=workspace)
    assert verdict.status == "unsure" and "ValueError" in verdict.reasons[0]


def test_llm_unavailable_is_unsure(setup, monkeypatch):
    state, workspace, _ = setup
    monkeypatch.setenv("LLM_OFFLINE", "1")
    verdict = fixers.run_fix(state, fixers.LLMFixer(), workspace=workspace)
    assert verdict.status == "unsure" and "LLMUnavailable" in verdict.reasons[0]


def test_slow_fixer_times_out(setup):
    state, workspace, _ = setup

    class Slow:
        name = "slow"

        def fix(self, workspace: Path, bug, failing_test, timeout_s):
            time.sleep(2)
            return CART_FIX.read_text()

    verdict = fixers.run_fix(state, Slow(), workspace=workspace, timeout_s=1)
    assert verdict.status == "unsure" and "timed out" in verdict.reasons[0] and state.diff == ""


def test_missing_inputs_unsure(setup):
    state, workspace, _ = setup
    state.failing_test = None
    assert fixers.run_fix(state, fixers.ScriptedFixer(CART_FIX), workspace=workspace).status == "unsure"


def test_normalize_diff_fixes_real_llm_output(tmp_path):
    """Real Kimi-K2.7-Code output: stray leading space before the header made git apply fail."""
    from factory.fixers import normalize_diff
    raw = (' --- a/app/cart.py\n+++ b/app/cart.py\n@@ -4,4 +4,4 @@\n \n def cart_total(cart_id: str) -> float:\n'
           '     cart = CARTS[cart_id]\n-    return sum(item["price"] * item["qty"] for item in cart["items"])\n'
           '+    return sum(item["price"] * item["qty"] for item in cart.get("items", []))\n')
    miscounted = raw.replace("@@ -4,4 +4,4 @@", "@@ -4,9 +4,2 @@")
    ws = StubWorkspace(make_mini_repo(tmp_path))
    for text in (raw, miscounted, "Here is the fix:\n```diff\n" + raw):
        diff = normalize_diff(text)
        assert diff.startswith("--- a/app/cart.py\n") and "@@ -4,4 +4,4 @@" in diff
        ws.apply_diff(ws.fresh_worktree("prod"), diff)   # raises if git rejects it
    ws.cleanup()
