"""B3 check: diff inspection rejects rogue diffs with the right reason and passes the real fixes."""
from pathlib import Path

import pytest

from factory.contracts import BugReport, RunState
from factory.sre_base import canonical_hash
from factory.stages import inspect_diff
from factory.stages.inspect_diff import CHECK_NAMES, inspect

FACTORY_FLOW = Path(__file__).resolve().parents[1]
TARGET = FACTORY_FLOW / "target_service"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

_FRAMEWORK = (
    '  File "/venv/lib/python3.12/site-packages/starlette/concurrency.py", line 37, in run_in_threadpool\n'
    "    return await anyio.to_thread.run_sync(func)\n"
)
# Same shape as the tracebacks the sre_plugin logs for the planted bugs (prod line numbers).
CART_TRACEBACK = (
    "Traceback (most recent call last):\n" + _FRAMEWORK
    + f'  File "{TARGET}/app/cart.py", line 34, in cart_total\n'
    '    total = sum(PRICES.get(item["sku"], 0.0) * item["qty"] for item in cart["items"])\n'
    "KeyError: 'items'\n"
)
ORDERS_TRACEBACK = (
    "Traceback (most recent call last):\n" + _FRAMEWORK
    + f'  File "{TARGET}/app/orders.py", line 47, in read_order\n'
    "    return get_order(order_id).to_dict()\n"
    "AttributeError: 'NoneType' object has no attribute 'to_dict'\n"
)
CART_TEST = (TARGET / "fallback_tests" / "test_repro_cart.py").read_text()
ORDERS_TEST = (TARGET / "fallback_tests" / "test_repro_orders.py").read_text()


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def cart_inspect(diff: str):
    return inspect(diff, traceback=CART_TRACEBACK, frozen_test=CART_TEST, worktree=TARGET)


def test_rogue_edits_test_is_rejected_for_protected_path():
    v = cart_inspect(fixture("rogue_edits_test.diff"))
    assert v.status == "fail"
    assert v.checks["no_protected_paths"] is False
    assert any(r.startswith("no_protected_paths") and "tests/test_service.py" in r for r in v.reasons)


def test_rogue_hardcode_is_rejected_for_frozen_test_literal():
    v = cart_inspect(fixture("rogue_hardcode.diff"))
    assert v.status == "fail"
    assert v.checks["no_frozen_test_literals"] is False
    assert any(r.startswith("no_frozen_test_literals") and "'dave'" in r for r in v.reasons)
    # every other rule holds: the literal rule alone catches it
    assert [k for k, ok in v.checks.items() if not ok] == ["no_frozen_test_literals"]


def test_rogue_adds_dependency_is_rejected_for_dependency_and_import():
    v = cart_inspect(fixture("rogue_adds_dependency.diff"))
    assert v.status == "fail"
    assert v.checks["no_dependency_files"] is False
    assert v.checks["no_banned_imports"] is False
    assert any(r.startswith("no_dependency_files") and "requirements.txt" in r for r in v.reasons)


def test_cart_patch_passes():
    diff = (TARGET / "patches" / "cart_fix.diff").read_text()
    v = cart_inspect(diff)
    assert v.status == "pass", v.reasons
    assert all(v.checks[name] for name in CHECK_NAMES)
    assert v.evidence == {"diff_sha256": canonical_hash(diff), "files": ["app/cart.py"], "lines_changed": 2}


def test_orders_patch_passes():
    v = inspect((TARGET / "patches" / "orders_helper_fix.diff").read_text(),
                traceback=ORDERS_TRACEBACK, frozen_test=ORDERS_TEST, worktree=TARGET)
    assert v.status == "pass", v.reasons
    assert v.evidence["files"] == ["app/orders.py"]


def test_canary_fixture_passes_inspection():
    v = cart_inspect(fixture("canary_breaks_endpoint.diff"))
    assert v.status == "pass", v.reasons


@pytest.mark.parametrize("diff", ["", "   \n", "hello world\n", "--- a/app/cart.py\n+++ b/app/cart.py\n",
                                  "--- a/app/cart.py\n+++ b/app/cart.py\n@@ -1,3 +1,3 @@\n-x\n+y\n"])
def test_empty_or_garbage_diff_fails(diff):
    v = cart_inspect(diff)
    assert v.status == "fail"
    assert v.checks["valid_unified_diff"] is False


def test_missing_traceback_is_unsure():
    v = inspect(fixture("canary_breaks_endpoint.diff"), traceback="", frozen_test=CART_TEST, worktree=TARGET)
    assert v.status == "unsure"
    assert v.checks["hunks_near_traceback"] is False
    assert any("hunks_near_traceback" in r and r.startswith("unsure") for r in v.reasons)


def test_missing_frozen_test_and_worktree_are_unsure():
    v = inspect((TARGET / "patches" / "cart_fix.diff").read_text(), traceback=CART_TRACEBACK)
    assert v.status == "unsure"
    assert v.checks["no_frozen_test_literals"] is False and v.checks["ast_parses"] is False


def test_violation_beats_missing_evidence():
    v = inspect(fixture("rogue_edits_test.diff"))
    assert v.status == "fail"


def test_far_hunk_secret_binary_and_syntax_error_fail():
    far = (TARGET / "patches" / "orders_helper_fix.diff").read_text()
    assert cart_inspect(far).checks["hunks_near_traceback"] is False          # orders.py not in cart traceback

    secret = fixture("rogue_hardcode.diff").replace('return {"user": "dave", "total": 0}',
                                                    'api_key = "sk-abcdefghijklmnopqrstuvwxyz"')
    assert cart_inspect(secret).checks["no_secrets"] is False

    binary = "diff --git a/app/logo.png b/app/logo.png\nBinary files a/app/logo.png and b/app/logo.png differ\n"
    assert cart_inspect(binary).checks["no_binary"] is False

    broken = (TARGET / "patches" / "cart_fix.diff").read_text().replace('cart.get("items", []))', 'cart.get("items", [])')
    v = cart_inspect(broken)
    assert v.status == "fail" and v.checks["ast_parses"] is False


def test_run_wrapper_reads_state(tmp_path):
    test_file = tmp_path / "test_repro.py"
    test_file.write_text(CART_TEST)
    bug = BugReport(id="BUG-1", alert_id="A1", signature="KeyError@app/cart.py:cart_total", exception_type="KeyError",
                    traceback=CART_TRACEBACK, sample_request={"method": "GET", "path": "/cart/total"},
                    files=["app/cart.py"], error_share=0.9, replay_failures=3, evidence_ids=[])
    state = RunState(run_id="r1", alert={}, artifacts_dir=tmp_path, bug=bug, failing_test=test_file,
                     diff=fixture("rogue_hardcode.diff"))
    v = inspect_diff.run(state)
    assert v.stage == "inspect_diff" and v.status == "fail" and v.checks["no_frozen_test_literals"] is False
