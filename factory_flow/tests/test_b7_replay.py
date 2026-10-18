"""B7 check: replay reproduces a promoted and a rejected run; tampering fails at the right seq."""
import hashlib
import hmac
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from factory import replay as replay_mod
from factory import workspace
from factory.replay import replay
from factory.sre_base import canonical_hash

FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(FIXTURES))
import gen_replay_runs  # noqa: E402

KEY = gen_replay_runs.TEST_HMAC_KEY
PROMOTED, REJECTED = gen_replay_runs.PROMOTED, gen_replay_runs.REJECTED


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    target = workspace.target_dir()
    if not (target / ".git").exists():
        subprocess.run([str(target / "scripts" / "init_repo.sh")], check=True)
    out = tmp_path_factory.mktemp("runs")
    data = gen_replay_runs.build(out)
    yield data
    workspace.cleanup()


@pytest.fixture
def runs(built, tmp_path, monkeypatch):
    """A private copy of the generated runs/ dir that a test may tamper with."""
    monkeypatch.setenv("AUDIT_HMAC_KEY", KEY)
    shutil.copytree(built["audit"].parent, tmp_path / "runs")
    return tmp_path / "runs"


def _replay(runs: Path, run_id: str, **kw) -> dict:
    return replay(run_id, audit_path=runs / "audit.jsonl", artifacts_root=runs / "artifacts", **kw)


def _entries(runs: Path) -> list[dict]:
    return [json.loads(l) for l in (runs / "audit.jsonl").read_text().splitlines() if l.strip()]


def _write(runs: Path, entries: list[dict]) -> None:
    (runs / "audit.jsonl").write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in entries))


def _seq_of(entries: list[dict], run_id: str, stage: str) -> int:
    return [e["seq"] for e in entries if e["run_id"] == run_id and e["stage"] == stage][-1]


def _rechain(entries: list[dict], start: int, key: str) -> None:
    """Recompute entry_hash/prev_hash/hmac from index `start` on (a forger who rewrites the tail)."""
    for i in range(start, len(entries)):
        e = entries[i]
        e["prev_hash"] = entries[i - 1]["entry_hash"] if i else ""
        e["entry_hash"] = canonical_hash({k: v for k, v in e.items() if k not in ("entry_hash", "hmac")})
        e["hmac"] = hmac.new(key.encode(), e["entry_hash"].encode(), hashlib.sha256).hexdigest()


def test_generated_runs_have_the_expected_recorded_verdicts(built):
    assert built["verdicts"][PROMOTED] == {"inspect_diff": "pass", "oracle": "pass"}
    assert built["verdicts"][REJECTED] == {"inspect_diff": "fail"}
    stages = [e["stage"] for e in built["entries"] if e["run_id"] == REJECTED]
    assert "oracle" not in stages and stages[-1] == "escalate"


def test_promoted_run_reproduces(runs, capsys):
    r = _replay(runs, PROMOTED)
    assert r["chain_ok"] and r["first_bad_seq"] is None
    assert r["reproduced"], r["reasons"]
    assert [(c["stage"], c["recorded"], c["replayed"]) for c in r["checks"]] == [
        ("inspect_diff", "pass", "pass"), ("oracle", "pass", "pass")]
    assert r["ref"] == "release-0"


def test_rejected_run_reproduces_without_running_oracle(runs, monkeypatch):
    monkeypatch.setattr(replay_mod.oracle, "check", lambda *a, **k: pytest.fail("oracle must not run"))
    r = _replay(runs, REJECTED)
    assert r["reproduced"], r["reasons"]
    assert [(c["stage"], c["recorded"], c["replayed"]) for c in r["checks"]] == [("inspect_diff", "fail", "fail")]


def test_cli_exit_codes(runs, capsys):
    args = ["--audit", str(runs / "audit.jsonl"), "--artifacts", str(runs / "artifacts")]
    assert replay_mod.main([REJECTED, *args]) == 0
    assert "decision reproduced" in capsys.readouterr().out
    assert replay_mod.main(["run_missing", *args]) == 1
    assert "decision NOT reproduced" in capsys.readouterr().out


def test_tampered_audit_line_fails_at_that_seq(runs, capsys):
    entries = _entries(runs)
    seq = _seq_of(entries, REJECTED, "inspect_diff")
    entries[seq - 1]["status"] = "pass"        # flip the rejection without re-hashing
    _write(runs, entries)
    for run_id in (PROMOTED, REJECTED):         # the whole chain is verified, whatever the run
        r = _replay(runs, run_id)
        assert not r["chain_ok"] and r["first_bad_seq"] == seq
        assert not r["reproduced"] and r["checks"] == []
    args = ["--audit", str(runs / "audit.jsonl"), "--artifacts", str(runs / "artifacts")]
    assert replay_mod.main([REJECTED, *args]) == 1
    out = capsys.readouterr().out
    assert f"chain: BROKEN at seq {seq}" in out and "decision NOT reproduced" in out


def test_rechained_forgery_without_the_key_fails_hmac_at_that_seq(runs):
    entries = _entries(runs)
    seq = _seq_of(entries, REJECTED, "inspect_diff")
    entries[seq - 1]["status"] = "pass"
    _rechain(entries, seq - 1, key="attacker-guess")
    _write(runs, entries)
    r = _replay(runs, REJECTED)
    assert not r["chain_ok"] and r["first_bad_seq"] == seq and not r["reproduced"]


def test_forged_verdict_with_valid_chain_is_a_mismatch(runs):
    """Even a forger holding the key cannot make replay agree: the gate is re-run and disagrees."""
    entries = _entries(runs)
    seq = _seq_of(entries, REJECTED, "inspect_diff")
    entries[seq - 1]["status"] = "pass"
    _rechain(entries, seq - 1, key=KEY)
    _write(runs, entries)
    r = _replay(runs, REJECTED)
    assert r["chain_ok"] and not r["reproduced"]
    assert r["checks"][0]["recorded"] == "pass" and r["checks"][0]["replayed"] == "fail"


def test_dropped_head_line_is_detected(runs):
    entries = _entries(runs)[1:]
    _write(runs, entries)
    r = _replay(runs, PROMOTED)
    assert not r["chain_ok"] and r["first_bad_seq"] == 2


def test_tampered_fix_diff_is_a_hash_mismatch(runs, monkeypatch):
    monkeypatch.setattr(replay_mod.oracle, "check", lambda *a, **k: pytest.fail("must refuse before re-running"))
    diff = runs / "artifacts" / PROMOTED / "fix.diff"
    diff.write_text(diff.read_text().replace('cart.get("items", [])', 'cart.get("items", []) or []'))
    r = _replay(runs, PROMOTED)
    assert r["chain_ok"] and not r["reproduced"] and r["checks"] == []
    assert any("diff_sha256 mismatch" in reason for reason in r["reasons"])


def test_tampered_frozen_test_is_a_hash_mismatch(runs):
    test = runs / "artifacts" / REJECTED / "test_repro_cart.py"
    test.write_text(test.read_text() + "\n# edited\n")
    r = _replay(runs, REJECTED)
    assert not r["reproduced"] and any("test_sha256 mismatch" in reason for reason in r["reasons"])


def test_missing_key_or_wrong_key_is_not_reproduced(runs, monkeypatch):
    monkeypatch.delenv("AUDIT_HMAC_KEY")
    r = _replay(runs, PROMOTED)
    assert not r["reproduced"] and "AUDIT_HMAC_KEY" in r["reasons"][0]
    r = _replay(runs, PROMOTED, hmac_key="wrong-key")
    assert not r["chain_ok"] and r["first_bad_seq"] == 1
