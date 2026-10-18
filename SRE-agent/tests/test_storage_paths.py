import pytest

from executor.gitops import GitOpsExecutor
from integrations.mock_mcp import MockMCP


def test_external_identifiers_cannot_escape_storage(tmp_path):
    for identifier in ("../outside", "/tmp/outside", "x/../../outside"):
        with pytest.raises(ValueError):
            MockMCP.seed_live_state(identifier, {})
        with pytest.raises(ValueError):
            MockMCP.reset_live_state(identifier)
    executor = GitOpsExecutor(repo_dir=str(tmp_path / "gitops"))
    incident = {"service": "api", "namespace": "prod"}
    for path in ("../outside.json", "/tmp/outside.json", "clusters/script.py"):
        with pytest.raises(ValueError):
            executor._manifest_path({"manifest_path": path}, incident)
    assert executor._manifest_path({"manifest_path": "clusters/api.json"}, incident).is_relative_to(tmp_path / "gitops")
