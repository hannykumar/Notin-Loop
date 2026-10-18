"""B4 check: the oracle passes both real patches, fails an empty diff and a test-breaking diff."""
import subprocess
from pathlib import Path

import pytest

from factory import workspace
from factory.contracts import RunState
from factory.sre_base import canonical_hash
from factory.stages import oracle

TARGET = workspace.TARGET_DIR
FIXTURES = Path(__file__).parent / "fixtures"
CART_FIX = (TARGET / "patches" / "cart_fix.diff").read_text()
ORDERS_FIX = (TARGET / "patches" / "orders_helper_fix.diff").read_text()
BREAKS_TEST = (FIXTURES / "oracle_breaks_test.diff").read_text()
REPRO_CART = TARGET / "fallback_tests" / "test_repro_cart.py"
REPRO_404 = TARGET / "fallback_tests" / "test_repro_404.py"


@pytest.fixture(scope="module", autouse=True)
def target_repo():
    if not (TARGET / ".git").exists():
        subprocess.run([str(TARGET / "scripts" / "init_repo.sh")], check=True)
    yield
    workspace.cleanup()


def test_diff_hash_is_canonical_hash_of_text():
    assert oracle.diff_hash(CART_FIX) == canonical_hash(CART_FIX)
    assert oracle.diff_hash(CART_FIX) != oracle.diff_hash(CART_FIX + "\n")


def test_cart_fix_passes(tmp_path):
    v = oracle.check(CART_FIX, REPRO_CART, artifacts_dir=tmp_path)
    assert v.stage == "oracle"
    assert v.status == "pass", v.reasons
    assert all(v.checks.values()) and set(v.checks) == set(oracle.CHECK_NAMES)
    assert v.evidence["diff_sha256"] == oracle.diff_hash(CART_FIX)
    assert len(v.evidence["test_sha256"]) == 64
    assert v.evidence["baseline"]["passed"] == v.evidence["patched"]["passed"] > 0
    assert v.evidence["flaky_excluded"] == []
    for entry in v.evidence["junit"].values():
        assert Path(entry["path"]).parent == tmp_path and len(entry["sha256"]) == 64
    assert v.evidence["junit_sha256"] == v.evidence["junit"]["patched_suite"]["sha256"]
    assert workspace.list_worktrees() == []


def test_orders_helper_fix_passes():
    v = oracle.check(ORDERS_FIX, REPRO_404)
    assert v.status == "pass", v.reasons


def test_empty_diff_fails_repro_still_red():
    v = oracle.check("", REPRO_CART)
    assert v.status == "fail"
    assert any("repro still red" in r for r in v.reasons), v.reasons
    assert v.checks["repro_red_on_baseline"] and not v.checks["repro_now_passes"]


def test_test_breaking_diff_fails_regression():
    v = oracle.check(BREAKS_TEST, REPRO_CART)
    assert v.status == "fail"
    assert v.checks["repro_now_passes"] and not v.checks["suite_still_green"]
    reason = next(r for r in v.reasons if r.startswith("regression"))
    assert "test_cart_total_for_existing_cart" in reason
    assert any("test_cart_total_for_existing_cart" in n for n in v.evidence["regressions"])


def test_garbage_diff_fails_does_not_apply():
    v = oracle.check("diff --git a/app/nope.py b/app/nope.py\n--- a/app/nope.py\n+++ b/app/nope.py\n@@ -1 +1 @@\n-x\n+y\n",
                     REPRO_CART)
    assert v.status == "fail"
    assert any("diff does not apply" in r for r in v.reasons)


def test_repro_green_on_baseline_is_unsure():
    v = oracle.check(CART_FIX, TARGET / "tests" / "test_service.py")
    assert v.status == "unsure"
    assert any("passes on baseline" in r for r in v.reasons)


def test_run_sets_state_hash(tmp_path):
    state = RunState(run_id="t", alert={}, artifacts_dir=tmp_path, failing_test=REPRO_CART, diff=CART_FIX)
    v = oracle.run(state)
    assert v.status == "pass", v.reasons
    assert state.diff_sha256 == oracle.diff_hash(CART_FIX)
