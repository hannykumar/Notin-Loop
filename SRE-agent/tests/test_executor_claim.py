from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from executor.schemas import ExecutionRequest
from executor.service import ExecutionService
from runtime.db import init_db, reset_db_state
from runtime.settings import reset_settings_cache


@pytest.mark.parametrize("operation", ["execute", "rollback"])
def test_duplicate_execution_is_claimed_before_external_action(monkeypatch, tmp_path, operation):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'executor.db'}")
    reset_settings_cache()
    reset_db_state()
    init_db()
    started, release = Event(), Event()
    calls = []

    def action(**kwargs):
        calls.append(kwargs)
        started.set()
        assert release.wait(5)
        return {"status": "completed", "command": "test", "execution_results": [{"status": "ok"}]}

    monkeypatch.setattr("executor.service.execute_action" if operation == "execute" else "executor.service.execute_rollback", action)
    invoke = getattr(ExecutionService(), operation)
    request = ExecutionRequest(execution_id="once", incident_id="INC002", incident={}, action={}, execution_mode="simulate", tool_mode="mock")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(invoke, request)
            assert started.wait(5)
            try:
                with pytest.raises(RuntimeError, match="in progress"):
                    invoke(request)
            finally:
                release.set()
            result = first.result()
        assert invoke(request) == result
        assert len(calls) == 1
        with pytest.raises(ValueError, match="different"):
            invoke(request.model_copy(update={"action": {"target": "other"}}))
    finally:
        release.set()
        reset_db_state()
        reset_settings_cache()
