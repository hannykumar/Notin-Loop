from integrations.mock_mcp import MockMCP


def setup_function() -> None:
    MockMCP.reset_live_state()


def test_read_tools_return_expected_shape() -> None:
    mcp = MockMCP("INC-001")
    status = mcp.get_pod_status(service="checkout", namespace="prod")
    metrics = mcp.get_metrics(service="checkout", namespace="prod")
    desc = mcp.describe_pod(pod_name=status["pod_name"], namespace="prod")
    logs = mcp.get_pod_logs(pod_name=status["pod_name"], namespace="prod", lines=50)

    assert status["pod_name"]
    assert status["restarts"] >= 0
    assert "memory_percent" in metrics
    assert desc["last_state"]["terminated"]["reason"] in {"OOMKilled", "Completed"}
    assert isinstance(logs, list)
    assert len(logs) > 0


def test_write_requires_approval() -> None:
    mcp = MockMCP("INC-001", allow_write=False)
    try:
        mcp.restart_pod(service="checkout", namespace="prod")
    except PermissionError:
        pass
    else:
        raise AssertionError("Expected PermissionError for write action without approval")


def test_restart_pod_mutates_state_and_reduces_error_signals() -> None:
    mcp = MockMCP("INC-002", allow_write=True)
    before_metrics = dict(mcp.get_metrics(service="api", namespace="prod"))
    before_logs = "\n".join(mcp.get_pod_logs("api-6fdfd5c8c9-k9z77", "prod", lines=200)).lower()

    out = mcp.restart_pod(service="api", namespace="prod")
    after_metrics = mcp.get_metrics(service="api", namespace="prod")
    after_logs = "\n".join(mcp.get_pod_logs("api-6fdfd5c8c9-k9z77", "prod", lines=200)).lower()

    assert out["status"] == "ok"
    assert after_metrics.get("error_rate_percent", 999) <= before_metrics.get("error_rate_percent", 999)
    assert after_logs.count("503") <= before_logs.count("503")


def test_reset_live_state_restores_original_incident() -> None:
    mcp = MockMCP("INC-002", allow_write=True)
    mcp.scale_deployment(service="api", namespace="prod", replicas=4)
    assert mcp.get_pod_status(service="api", namespace="prod").get("replicas") == 4

    MockMCP.reset_live_state("INC-002")
    fresh = MockMCP("INC-002")
    assert fresh.get_pod_status(service="api", namespace="prod").get("replicas") != 4


def test_execute_remediation_runs_structured_action() -> None:
    mcp = MockMCP("INC-003", allow_write=True)
    out = mcp.execute_remediation(
        {
            "action_type": "restart_coredns",
            "target": "coredns",
            "namespace": "kube-system",
            "reason": "DNS failures confirmed",
        }
    )

    assert out["status"] == "ok"
    assert out["tool"] == "execute_remediation"
    assert "kubectl rollout restart deployment/coredns" in out["command"]
