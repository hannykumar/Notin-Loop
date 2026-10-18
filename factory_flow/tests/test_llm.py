"""A1: LLM client with the OpenAI SDK mocked (no live call: no key in this environment)."""
from types import SimpleNamespace

import pytest

from factory import llm


class FakeClient:
    def __init__(self, reply=None, error=None, **kwargs):
        self.kwargs, self.reply, self.error, self.calls = kwargs, reply, error, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))])


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://api.studio.nebius.ai/v1/")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.delenv("LLM_OFFLINE", raising=False)


def use_client(monkeypatch, **kwargs) -> list:
    made = []
    monkeypatch.setattr(llm, "OpenAI", lambda **kw: made.append(FakeClient(**kwargs, **kw)) or made[-1])
    return made


def test_fences_stripped_and_success_recorded(env, monkeypatch):
    made = use_client(monkeypatch, reply="Here:\n```python\nprint('hi')\n```\nthanks")
    assert llm.complete("sys", "user") == "print('hi')"
    client = made[0]
    assert client.kwargs == {"base_url": "https://api.studio.nebius.ai/v1/", "api_key": "test-key",
                             "timeout": 60, "max_retries": 1}
    assert client.calls[0]["model"] == "test-model" and client.calls[0]["temperature"] == 0.1
    status = llm.status()
    assert status["provider"] == "nebius" and status["model"] == "test-model"
    assert status["last_success_at"] > 0 and status["last_error"] == ""
    assert "test-key" not in str(status)


def test_plain_text_passes_through(env, monkeypatch):
    use_client(monkeypatch, reply="\njust text\n")
    assert llm.complete("s", "u") == "just text"


def test_offline_raises_without_client(env, monkeypatch):
    monkeypatch.setenv("LLM_OFFLINE", "1")
    made = use_client(monkeypatch, reply="x")
    with pytest.raises(llm.LLMUnavailable):
        llm.complete("s", "u")
    assert made == []


def test_missing_config_raises(env, monkeypatch):
    monkeypatch.delenv("LLM_MODEL")
    made = use_client(monkeypatch, reply="x")
    with pytest.raises(llm.LLMUnavailable):
        llm.complete("s", "u")
    assert made == []


def test_api_error_wrapped_and_recorded(env, monkeypatch):
    use_client(monkeypatch, error=ConnectionError("boom"))
    with pytest.raises(llm.LLMUnavailable, match="boom"):
        llm.complete("s", "u")
    status = llm.status()
    assert status["last_failure_at"] > 0 and "boom" in status["last_error"]
