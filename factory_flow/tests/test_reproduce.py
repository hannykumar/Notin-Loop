from pathlib import Path

from factory import llm, sre_base
from factory.contracts import BugReport, RunState
from factory.stages import reproduce
from track_a_support import FIXTURES, StubWorkspace, make_mini_repo

GOOD = '''```python
from fastapi.testclient import TestClient

from app.main import app


def test_empty_cart():
    response = TestClient(app).get("/cart/total", params={"cart_id": "empty"})
    assert response.status_code < 500
```'''
PASSES_ON_BUG = GOOD.replace('"empty"', '"c1"')
ASSERT_FALSE = GOOD.replace("assert response.status_code < 500", "assert False")
IMPORT_ERROR = GOOD.replace("from app.main import app", "from app.main import app\nfrom app.nope import x")

BUG = BugReport(
    id="BUG-CART", alert_id="a1", signature="KeyError@app/cart.py:cart_total", exception_type="KeyError",
    traceback="KeyError: 'items'", sample_request={"method": "GET", "path": "/cart/total", "query": {"cart_id": "empty"},
                                                    "body": None},
    files=["app/cart.py"], error_share=1.0, replay_failures=3, evidence_ids=["e1"],
)


def scripted(*replies):
    calls = []

    def complete(system, user):
        calls.append(user)
        reply = replies[len(calls) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply
    complete.calls = calls
    return complete


def go(tmp_path, complete, bug=BUG, fallback_dir=None):
    state = RunState(run_id="r1", alert={}, artifacts_dir=tmp_path / "artifacts", bug=bug)
    v = reproduce.run(state, workspace=StubWorkspace(make_mini_repo(tmp_path)), llm_complete=complete,
                      fallback_dir=fallback_dir or tmp_path / "no_fallback")
    return state, v


def test_valid_fenced_test_is_frozen(tmp_path):
    state, v = go(tmp_path, scripted(GOOD))
    assert v.status == "pass", v.reasons
    frozen = Path(v.evidence["test_path"])
    assert frozen == state.failing_test == tmp_path / "artifacts" / "test_repro_bug_cart.py"
    assert "```" not in frozen.read_text()
    assert state.test_sha256 == v.evidence["test_sha256"] == sre_base.canonical_hash(frozen.read_text())
    assert [r["failure_types"] for r in v.evidence["runs"]] == [["KeyError"]] * 3
    assert v.evidence["attempts"] == 1 and v.evidence["fallback_used"] is False and "llm" in v.evidence


def test_green_on_buggy_code_retries(tmp_path):
    complete = scripted(PASSES_ON_BUG, GOOD)
    state, v = go(tmp_path, complete)
    assert v.status == "pass" and v.evidence["attempts"] == 2
    assert "not a valid red" in v.evidence["rejections"][0]
    assert "previous test was rejected" in complete.calls[1]


def test_assert_false_twice_is_unsure(tmp_path):
    state, v = go(tmp_path, scripted(ASSERT_FALSE, ASSERT_FALSE))
    assert v.status == "unsure" and state.failing_test is None
    assert all("constant false" in r for r in v.evidence["rejections"])


def test_import_error_is_not_a_valid_red(tmp_path):
    state, v = go(tmp_path, scripted(IMPORT_ERROR, IMPORT_ERROR))
    assert v.status == "unsure" and state.failing_test is None
    assert all("errors=1" in r for r in v.evidence["rejections"])


def test_llm_unavailable_uses_fallback(tmp_path):
    state, v = go(tmp_path, scripted(llm.LLMUnavailable("LLM_OFFLINE=1")), fallback_dir=FIXTURES / "a9_fallback_tests")
    assert v.status == "pass", v.reasons
    assert v.evidence["fallback_used"] is True
    assert state.failing_test.read_text() == (FIXTURES / "a9_fallback_tests" / "test_repro_cart.py").read_text()


def test_llm_offline_without_fallback_is_unsure(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_OFFLINE", "1")
    state, v = go(tmp_path, None)  # real llm.complete raises LLMUnavailable, no network
    assert v.status == "unsure" and "no fallback" in v.reasons[0]


def test_missing_bug_is_unsure(tmp_path):
    state = RunState(run_id="r1", alert={}, artifacts_dir=tmp_path / "artifacts")
    assert reproduce.run(state, workspace=object()).status == "unsure"


def test_lint_rejects_banned_patterns():
    for bad in ["import requests", "import time\ntime.sleep(1)", "pytest.skip('x')", "open('f', 'w')",
                "import subprocess", "os.system('ls')"]:
        assert reproduce.lint(llm.strip_fences(GOOD) + "\n" + bad), bad
    assert reproduce.lint(llm.strip_fences(GOOD)) == ""
