import json
from integrations.live_backends import KubectlLiveAdapter, PrometheusAdapter, LiveSREBackend
from runtime.settings import reset_settings_cache


def test_pod_selection_checks_unhealthy_replicas(monkeypatch):
    pods = {"items": [
        {"metadata": {"name": "api-healthy"}, "status": {"containerStatuses": [{"restartCount": 0, "state": {"running": {}}}]}},
        {"metadata": {"name": "api-crashing"}, "status": {"containerStatuses": [{"restartCount": 3, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}
    ]}
    adapter = KubectlLiveAdapter()
    monkeypatch.setattr(adapter, "_run", lambda *args: json.dumps(pods))
    result = adapter.get_pod_status("api", "prod")
    assert result["pod_name"] == "api-crashing"
    assert result["replicas"] == 2
    assert result["reason"] == "CrashLoopBackOff"


def test_prometheus_nan_means_missing_observation(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_BASE_URL", "http://prometheus.example")
    reset_settings_cache()
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"data":{"result":[{"value":[0,"NaN"]}]}}'
    monkeypatch.setattr("integrations.live_backends.urllib.request.urlopen", lambda *args, **kwargs: Response())
    assert PrometheusAdapter()._query("up") is None
    reset_settings_cache()


def test_live_trace_query_never_substitutes_fixture_traces(monkeypatch):
    monkeypatch.delenv("TEMPO_BASE_URL", raising=False)
    result = LiveSREBackend().query_tempo("api", "prod")
    assert result["spans"] == []
    assert result["status"] == "not_configured"
