import pytest

from integrations.live_backends import KubectlLiveAdapter, LiveSREBackend
from agent.deterministic_policy import build_action


@pytest.mark.parametrize("method,args", [
    ("get_pod_status", {"service": "api,app!=private", "namespace": "prod"}),
    ("describe_pod", {"pod_name": "--all", "namespace": "prod"}),
    ("get_pod_logs", {"pod_name": "api-1", "namespace": "--all-namespaces"}),
    ("get_pod_logs", {"pod_name": "api-1", "namespace": "prod", "lines": -1}),
    ("get_cluster_events", {"namespace": "--all-namespaces"}),
    ("get_deployment", {"service": "--all", "namespace": "prod"}),
])
def test_invalid_read_arguments_never_reach_kubectl(monkeypatch, method, args):
    adapter = KubectlLiveAdapter()
    def forbidden(*args):
        pytest.fail("Invalid arguments reached kubectl")
    monkeypatch.setattr(adapter, "_run", forbidden)
    with pytest.raises(ValueError):
        getattr(adapter, method)(**args)


def test_dependency_reads_use_the_incident_namespace(monkeypatch):
    monkeypatch.setattr("integrations.live_backends.service_context", lambda service: {
        "namespace": "old-default", "dependencies": ["payments"],
    })
    calls = []
    class Kubernetes:
        def get_deployment(self, **kwargs):
            calls.append(kwargs)
            return {"replicas": 0, "ready_replicas": 0}
    LiveSREBackend(kubectl=Kubernetes()).get_service_dependencies("api", "incident-namespace")
    assert calls == [{"service": "payments", "namespace": "incident-namespace"}]


@pytest.mark.parametrize("replicas", [None, 2])
def test_unready_dependency_is_not_assumed_to_be_scaled_to_zero(replicas):
    action = build_action({"service": "api", "namespace": "prod"}, "Service503", {
        "service_dependencies": {"unavailable_count": 1, "unavailable": [
            {"name": "payments", "replicas": replicas, "ready_replicas": 0},
        ]},
    })
    assert action is None
