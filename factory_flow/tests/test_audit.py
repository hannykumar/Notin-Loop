"""A2: hash chain + HMAC audit log."""
import json

import pytest

from factory import audit


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("AUDIT_HMAC_KEY", "test-key")


def _write_five():
    for i in range(5):
        audit.append({"run_id": "run_t", "stage": f"s{i}", "status": "pass", "reasons": [], "checks": {}, "evidence": {}})


def test_five_entries_verify():
    _write_five()
    entries = audit.read("run_t")
    assert [e["seq"] for e in entries] == [1, 2, 3, 4, 5]
    assert entries[0]["prev_hash"] == "" and entries[1]["prev_hash"] == entries[0]["entry_hash"]
    assert entries[0]["actor"] == "agent"
    assert audit.verify() == (True, None)


def test_edit_line_3_reports_seq_3():
    _write_five()
    path = audit.audit_path()
    lines = path.read_text().splitlines()
    entry = json.loads(lines[2])
    entry["status"] = "fail"
    lines[2] = json.dumps(entry)
    path.write_text("\n".join(lines) + "\n")
    assert audit.verify() == (False, 3)


def test_garbled_line_is_bad_at_its_position():
    _write_five()
    path = audit.audit_path()
    lines = path.read_text().splitlines()
    lines[3] = "{not json"
    path.write_text("\n".join(lines) + "\n")
    assert audit.verify() == (False, 4)


def test_wrong_key_fails(monkeypatch):
    _write_five()
    monkeypatch.setenv("AUDIT_HMAC_KEY", "other-key")
    assert audit.verify() == (False, 1)


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("AUDIT_HMAC_KEY")
    with pytest.raises(RuntimeError, match="AUDIT_HMAC_KEY"):
        audit.append({"run_id": "run_t", "stage": "x"})
