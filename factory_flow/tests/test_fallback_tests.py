"""Offline demo path: each fallback repro test is found by A9's naming rule, passes its lint, is red on prod
for the right reason, and green with the scripted patch. (Found in the LLM_OFFLINE rehearsal: the old
fallback tests used a conftest fixture instead of importing app.main, and the orders one was misnamed.)"""
from __future__ import annotations

import pytest

from factory import workspace
from factory.stages import reproduce

CASES = [  # (bug file, exception the bug raises, scripted patch)
    ("app/cart.py", "KeyError", "cart_fix.diff"),
    ("app/orders.py", "AttributeError", "orders_helper_fix.diff"),
]


@pytest.mark.parametrize("bug_file,exc,patch", CASES)
def test_fallback_is_valid_red_then_green(bug_file, exc, patch):
    target = workspace.target_dir()
    name = f"test_repro_{bug_file.split('/')[-1][:-3]}.py"           # A9 naming: stem of bug.files[0]
    text = (target / "fallback_tests" / name).read_text()
    assert not reproduce.lint(text), reproduce.lint(text)

    tree = workspace.fresh_worktree("prod")
    try:
        (tree / "tests" / name).write_text(text)
        red = workspace.run_pytest(tree, f"tests/{name}")
        assert red["failed"] >= 1 and red["errors"] == 0 and red["passed"] == 0, red
        assert {f["type"] for f in red["failures"]} <= {exc, "AssertionError"}, red["failures"]
        assert workspace.apply_diff(tree, (target / "patches" / patch).read_text())["applied"]
        green = workspace.run_pytest(tree, f"tests/{name}")
        assert green["passed"] >= 1 and green["failed"] == 0 and green["errors"] == 0, green
    finally:
        workspace.remove_worktree(tree)
