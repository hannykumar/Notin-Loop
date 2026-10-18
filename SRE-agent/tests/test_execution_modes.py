import pytest

from executor.engine import execute_action, execute_rollback


@pytest.mark.parametrize("execute", [execute_action, execute_rollback])
def test_preview_never_calls_a_write_gateway(monkeypatch, execute):
    def forbidden(*args, **kwargs):
        raise AssertionError("Preview reached a write gateway")

    monkeypatch.setattr("executor.engine.GitOpsExecutor", forbidden)
    monkeypatch.setattr("executor.engine.KubectlMCPClient", forbidden)
    monkeypatch.setattr("executor.engine.get_tool_client", forbidden)
    args = dict(run_id="preview-test", execution_mode="preview", tool_mode="mcp", incident_id="INC-007",
                incident={"service": "api", "namespace": "prod"},
                action={"action_type": "gitops_rollback_deployment", "target": "api", "previous_version": "v1"})
    if execute is execute_action:
        args["evidence_before"] = {}
    assert execute(**args)["status"] == "preview"
