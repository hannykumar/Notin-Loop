"""K2 check: policy rules use SRE-agent's real rule format, and the scope regexes behave."""
from dataclasses import asdict

from factory import policy, sre_base  # noqa: F401  (sre_base puts SRE-agent on sys.path)
from factory.contracts import BugReport, Verdict

from agent.evidence_graph import evaluate_rule  # real SRE-agent evaluator, test-only import


def bug(exception_type: str, traceback: str = "", files=("app/cart.py",), replay_failures: int = 3) -> dict:
    return asdict(BugReport(
        id="BUG-1", alert_id="A1", signature=f"{exception_type}@{files[0]}", exception_type=exception_type,
        traceback=traceback, sample_request={"method": "GET", "path": "/cart/total"}, files=list(files),
        error_share=0.9, replay_failures=replay_failures, evidence_ids=["E000", "E001"],
    ))


def matching_rules(payload: dict) -> list[str]:
    return [r["rule_id"] for r in policy.BUG_CLASSES if evaluate_rule(payload, r)]


def test_bug_classes_match_with_sre_agent_evaluator():
    assert matching_rules(bug("KeyError")) == ["missing_key"]
    assert matching_rules(bug("AttributeError", "AttributeError: 'NoneType' object has no attribute 'items'")) == ["none_access"]
    assert matching_rules(bug("RuntimeError")) == []                      # unknown class => unsure
    assert matching_rules(bug("KeyError", replay_failures=2)) == []       # intermittent => no class
    assert matching_rules(bug("KeyError", files=("lib/x.py",))) == []     # outside app/


def test_scope_regexes():
    for text in ["race in cache refresh", "ConcurrentModification", "intermittent", "auth token", "payment"]:
        assert policy.DENY_KEYWORDS.search(text), text
    assert not policy.DENY_KEYWORDS.search("KeyError@app/cart.py:cart_total")

    for path in ["tests/test_cart.py", "requirements.txt", "pyproject.toml", "patches/cart_fix.diff",
                 ".github/workflows/ci.yml", "fallback_tests/test_repro.py", "traffic/normal.json"]:
        assert policy.PROTECTED_PATHS.search(path), path
    assert not policy.PROTECTED_PATHS.search("app/cart.py")

    assert policy.DEPENDENCY_FILES.search("requirements.txt") and not policy.DEPENDENCY_FILES.search("app/cart.py")
    assert policy.BANNED_IMPORTS.search("+import subprocess") and policy.BANNED_IMPORTS.search("eval(x)")
    assert not policy.BANNED_IMPORTS.search("ast.literal_eval(x)")
    assert any(p.search('API_KEY = "abcdef123456"') for p in policy.SECRET_PATTERNS)
    assert not any(p.search("total = sum(prices)") for p in policy.SECRET_PATTERNS)
    assert any(p.match("/orders/42") for p in policy.SAFE_REPLAY_PATHS)
    assert not any(p.match("/orders/42/cancel") for p in policy.SAFE_REPLAY_PATHS)


def test_verdict_and_classify_shapes():
    v = Verdict(stage="triage", status="fail", reasons=["own release"], checks={"no_own_release": False}, evidence={}, route="rollback")
    assert v.route == "rollback"
    stable = {"metrics": {"error_rate_percent": 0.0, "p95_latency_ms": 20.0}}
    canary = {"metrics": {"error_rate_percent": 0.5, "p95_latency_ms": 21.0}}
    assert sre_base.classify_verification_outcome(stable, canary, {}) in policy.SLO_FORBIDDEN_OUTCOMES
