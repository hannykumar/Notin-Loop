import pytest
from pydantic import ValidationError
from integrations.kubectl_mcp_server import PodReq, ScaleReq, ServiceReq


@pytest.mark.parametrize("model, values", [
    (PodReq, {"pod_name": "--all"}),
    (ScaleReq, {"deployment": "--all", "replicas": 1}),
    (ServiceReq, {"service": "api,app!=payments"}),
])
def test_kubernetes_identifiers_reject_options_and_selectors(model, values):
    with pytest.raises(ValidationError):
        model(namespace="prod", **values)
    assert ScaleReq(namespace="prod", deployment="payments", replicas=0).replicas == 0
