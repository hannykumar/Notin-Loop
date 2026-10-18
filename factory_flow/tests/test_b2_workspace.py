"""B2 check: a prod worktree runs its suite green, takes a patch, and serves on a spare port."""
import json
import subprocess
import urllib.request
from pathlib import Path

import pytest

from factory import workspace

SPARE_PORT = 8201  # never 8101/8102 (policy STABLE/CANARY ports are used by other sessions)
CART_FIX = (workspace.target_dir() / "patches" / "cart_fix.diff").read_text()
CART_REPRO = "fallback_tests/test_repro_cart.py"


@pytest.fixture(scope="module", autouse=True)
def target_repo():
    if not (workspace.target_dir() / ".git").exists():
        subprocess.run([str(workspace.target_dir() / "scripts" / "init_repo.sh")], check=True)
    yield
    workspace.cleanup()


@pytest.fixture()
def prod():
    path = workspace.fresh_worktree("prod")
    try:
        yield path
    finally:
        workspace.cleanup()


def test_prod_worktree_suite_is_green(prod: Path):
    assert prod.is_dir() and (prod / "app" / "main.py").exists()
    assert prod.resolve() != workspace.target_dir().resolve()
    result = workspace.run_pytest(prod, "tests")
    assert (result["passed"], result["failed"], result["errors"]) == (16, 0, 0), result
    assert result["exit_code"] == 0 and result["failures"] == []
    assert Path(result["junit_path"]).exists()


def test_repro_red_before_patch_green_after(prod: Path):
    before = workspace.run_pytest(prod, CART_REPRO)
    assert (before["failed"], before["errors"], before["passed"]) == (1, 0, 0), before
    failure = before["failures"][0]
    assert failure["nodeid"] == f"{CART_REPRO}::test_cart_total_for_user_without_cart_is_zero"
    assert failure["type"] == "AssertionError" and "500" in failure["message"]

    assert workspace.apply_diff(prod, CART_FIX) == {"applied": True, "error": ""}
    after = workspace.run_pytest(prod, CART_REPRO)
    assert (after["passed"], after["failed"], after["errors"]) == (1, 0, 0), after
    # the main checkout is untouched (patched copy never leaks back)
    assert "cart.get(\"items\", [])" not in (workspace.target_dir() / "app" / "cart.py").read_text()


def test_apply_diff_rejects_garbage_without_raising(prod: Path):
    for bad in ("", "not a diff at all\n", CART_FIX.replace("cart[\"items\"]", "cart[\"nope\"]")):
        out = workspace.apply_diff(prod, bad)
        assert out["applied"] is False and out["error"], out
    assert workspace.apply_diff(prod, CART_FIX)["applied"] is True
    assert workspace.apply_diff(prod, CART_FIX)["applied"] is False  # already applied => check fails


def test_collection_error_counts_as_error(prod: Path):
    (prod / "tests" / "test_broken.py").write_text("import nonexistent_module_xyz\n")
    result = workspace.run_pytest(prod, "tests/test_broken.py")
    assert result["errors"] == 1 and result["failed"] == 0
    assert result["failures"][0]["type"] == "ModuleNotFoundError"
    missing = workspace.run_pytest(prod, "tests/does_not_exist.py")
    assert missing["errors"] == 1 and missing["passed"] == 0


def test_instance_serves_on_spare_port_and_stops(prod: Path, tmp_path: Path):
    assert not workspace.port_busy(SPARE_PORT), "spare port already in use"
    log = tmp_path / "server.log"
    try:
        info = workspace.start_instance(prod, SPARE_PORT, "canary", log_path=log)
        assert info["port"] == SPARE_PORT and "canary" in workspace.instances()
        with urllib.request.urlopen(f"http://127.0.0.1:{SPARE_PORT}/", timeout=2) as resp:
            assert json.loads(resp.read()) == {"ok": "true", "instance": "canary"}
        with pytest.raises(RuntimeError, match="busy|already running"):
            workspace.start_instance(prod, SPARE_PORT, "other", log_path=log)
    finally:
        assert workspace.stop_instance("canary") is True
    assert not workspace.port_busy(SPARE_PORT)
    assert workspace.stop_instance("canary") is False
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert lines and all(e["instance"] == "canary" for e in lines)


def test_default_log_path_is_this_checkouts_runs_dir(monkeypatch):
    monkeypatch.delenv("FACTORY_RUNS_DIR", raising=False)
    monkeypatch.delenv("FACTORY_TARGET_SERVICE", raising=False)
    assert workspace.server_log() == Path(workspace.__file__).resolve().parents[1] / "runs" / "server.log"
    assert workspace.target_dir() == Path(workspace.__file__).resolve().parents[1] / "target_service"


def test_cleanup_removes_worktrees():
    paths = [workspace.fresh_worktree("prod"), workspace.fresh_worktree("release-0")]
    try:
        assert all(p.exists() for p in paths)
    finally:
        workspace.cleanup()
    assert workspace.list_worktrees() == []
    assert not any(p.exists() for p in paths)
    listed = subprocess.run(["git", "worktree", "list"], cwd=workspace.target_dir(),
                            capture_output=True, text=True, check=True).stdout
    assert all(str(p) not in listed for p in paths)
    assert len(listed.strip().splitlines()) == 1
