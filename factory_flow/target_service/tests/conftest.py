from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cart
from app.main import app


@pytest.fixture(autouse=True)
def isolated_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_file = tmp_path / "server.log"
    monkeypatch.setenv("SRE_LOG_PATH", str(log_file))
    monkeypatch.delenv("SRE_INSTANCE", raising=False)
    return log_file


@pytest.fixture(autouse=True)
def fresh_state() -> None:
    cart.reset()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)
