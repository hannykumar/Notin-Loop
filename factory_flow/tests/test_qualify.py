"""A8: qualify checks against BUG-CART / BUG-404-like reports, on a temp git repo of mini_service."""
from factory import sre_base  # noqa: F401  (puts SRE-agent on sys.path for the parity check)
from factory.contracts import BugReport, RunState
from factory.stages import qualify
from track_a_support import StubWorkspace, git, make_mini_repo

from agent import evidence_graph  # real SRE-agent evaluator, test-only parity check

CART_TB = """Traceback (most recent call last):
  File "/venv/lib/python3.12/site-packages/starlette/concurrency.py", line 42, in run_in_threadpool
    return await anyio.to_thread.run_sync(func)
  File "/srv/target_service/app/cart.py", line 12, in cart_total
    return sum(PRICES[item] for item in cart)
KeyError: 'widget'"""

NONE_TB = """Traceback (most recent call last):
  File "app/orders.py", line 9, in order_status
    return get_order(order_id).status
AttributeError: 'NoneType' object has no attribute 'status'"""


def bug(exc="KeyError", tb=CART_TB, files=("app/cart.py",), path="/cart/total", fn="cart_total"):
    return BugReport(id="BUG-1", alert_id="A1", signature=f"{exc}@{files[0]}:{fn}", exception_type=exc,
                     traceback=tb, sample_request={"method": "GET", "path": path}, files=list(files),
                     error_share=0.9, replay_failures=3, evidence_ids=["E000", "E001"])


def run(tmp_path, report, repo=None):
    if repo is None:
        repo = tmp_path / "target_service"
        repo = repo if repo.exists() else make_mini_repo(tmp_path)
    ws = StubWorkspace(repo)
    state = RunState(run_id="r1", alert={}, artifacts_dir=tmp_path, bug=report)
    verdict = qualify.run(state, workspace=ws)
    assert not ws._trees  # worktrees always cleaned up
    return verdict


def test_bug_cart_passes_despite_framework_concurrency_frame(tmp_path):
    v = run(tmp_path, bug())
    assert v.status == "pass", v.reasons
    assert v.evidence["rule_id"] == "missing_key" and v.evidence["flaky_tests"] == [] and all(v.checks.values())


def test_bug_404_passes(tmp_path):
    v = run(tmp_path, bug("AttributeError", NONE_TB, ("app/orders.py",), "/orders/404", "order_status"))
    assert v.status == "pass" and v.evidence["rule_id"] == "none_access"


def test_scope_and_deny_failures(tmp_path):
    assert run(tmp_path, bug(files=("requirements.txt",))).status == "fail"
    assert run(tmp_path, bug(files=("app/cart.py", "app/orders.py", "app/main.py"))).status == "fail"
    race_tb = CART_TB.replace("/srv/target_service/app/cart.py", "app/stock.py").replace("cart_total", "refresh_race")
    v = run(tmp_path, bug(tb=race_tb, files=("app/stock.py",), fn="available"))
    assert v.status == "fail" and v.evidence["deny_match"] == "race"


def test_unknown_class_and_missing_bug_are_unsure(tmp_path):
    assert run(tmp_path, bug("RuntimeError", "RuntimeError: boom")).reasons == ["bug class not in policy"]
    assert run(tmp_path, bug("RuntimeError", "RuntimeError: boom")).status == "unsure"
    assert run(tmp_path, None).status == "unsure"


def test_red_prod_suite_fails(tmp_path):
    repo = make_mini_repo(tmp_path)
    git(repo, "checkout", "-q", "prod")
    (repo / "tests" / "test_red.py").write_text("def test_red():\n    assert 1 == 2\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=h@x", "-c", "user.name=h", "commit", "-q", "-m", "red")
    v = run(tmp_path, bug(), repo)
    assert v.status == "fail" and v.checks["prod_suite_green"] is False


def test_flaky_test_is_recorded_and_fails(tmp_path):
    class Flaky(StubWorkspace):
        calls = 0

        def run_pytest(self, path, selector=""):
            Flaky.calls += 1
            fail = [{"nodeid": "tests/test_basic.py::test_x", "type": "AssertionError", "message": ""}]
            return {"passed": 2, "failed": Flaky.calls % 2, "errors": 0, "skipped": 0,
                    "failures": fail if Flaky.calls % 2 else [], "junit_path": "", "exit_code": 0}

    ws = Flaky(make_mini_repo(tmp_path))
    v = qualify.run(RunState(run_id="r1", alert={}, artifacts_dir=tmp_path, bug=bug()), workspace=ws)
    assert v.status == "fail" and v.evidence["flaky_tests"] == ["tests/test_basic.py::test_x"] and not ws._trees


def test_port_matches_sre_agent_evaluator():
    from dataclasses import asdict
    from factory import policy
    for report in [bug(), bug("AttributeError", NONE_TB, ("app/orders.py",)), bug("RuntimeError")]:
        payload = asdict(report)
        for rule in policy.BUG_CLASSES:
            assert qualify.evaluate_rule(payload, rule) == evidence_graph.evaluate_rule(payload, rule)
