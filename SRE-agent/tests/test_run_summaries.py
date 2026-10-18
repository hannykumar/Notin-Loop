import pytest

from agent.run_summaries import build_verification_summary
from executor.engine import assess_verification_rules


@pytest.mark.parametrize("status", ["inconclusive", "verification_timeout", "regressed"])
def test_verification_summary_preserves_unconfirmed_or_failed_recovery(status):
    summary = build_verification_summary({
        "execution_results": [{"status": "executed"}],
        "verification": {"status": status},
        "improved": True,
    })
    assert summary["outcome"] == status


def test_preview_is_not_a_recovery_measurement():
    assert build_verification_summary({
        "execution_mode": "preview", "execution_results": [{"status": "preview"}],
    })["outcome"] == "not_run"


def test_relative_improvement_without_recovery_threshold_is_not_resolved():
    result = assess_verification_rules("DeploymentRegression", {
        "metrics": {"error_rate_percent": 90},
    }, {"metrics": {"error_rate_percent": 80}, "logs_tail": []})
    assert result["status"] == "improved"
    assert result["resolved"] is False
