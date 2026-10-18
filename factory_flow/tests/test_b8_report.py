"""B8 check: the report summarises the three sample demos, renders HTML and the rich view, and catches tampering."""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from factory import report

FIXTURES = Path(__file__).parent / "fixtures"
AUDIT = FIXTURES / "sample_audit.jsonl"
RELEASES = FIXTURES / "sample_releases.json"
TEST_KEY = "test-audit-key"   # same as gen_sample_audit.TEST_HMAC_KEY
NOW = datetime(2026, 10, 18, 15, 0, 0, tzinfo=timezone.utc)


def sample_summary():
    return report.summarize(report.load_audit(AUDIT), report.load_releases(RELEASES), now=NOW)


def test_fixture_chain_verifies_with_hmac():
    entries = report.load_audit(AUDIT)
    assert report.verify_chain(entries, TEST_KEY) == (True, None)
    assert report.verify_chain(entries, "wrong-key") == (False, 1)


def test_summarize_outcomes_and_scoreboard():
    s = sample_summary()
    runs = {r["run_id"]: r for r in s["runs"]}
    assert [r["run_id"] for r in s["runs"]] == ["run_cart01", "run_stock02", "run_orders03"]
    assert runs["run_cart01"]["outcome"] == "kept"
    assert [x["stage"] for x in runs["run_cart01"]["stages"]] == report.STAGES[:-1]
    assert all(x["status"] == "pass" for x in runs["run_cart01"]["stages"])
    assert runs["run_stock02"]["outcome"] == "escalated"
    assert runs["run_stock02"]["stopped_at"] == "investigate"
    assert runs["run_stock02"]["stages"][-1]["status"] == "unsure"
    assert runs["run_stock02"]["diff"] == ""
    assert runs["run_orders03"]["outcome"] == "rolled_back"
    assert runs["run_orders03"]["stopped_at"] == "watch"
    assert runs["run_orders03"]["inbox_case"].endswith("run_orders03.json")
    assert "get_order" in runs["run_orders03"]["diff"]
    assert s["scoreboard"] == {"runs": 3, "promoted_kept": 1, "promoted_watching": 0, "escalated": 1,
                               "rolled_back": 1, "releases": 2, "releases_watching": 0, "bad_changes_left": 0}


def test_bad_changes_counts_watching_release_past_window():
    releases = report.load_releases(RELEASES)
    releases.append({**releases[0], "tag": "release-3", "run_id": "run_x", "status": "watching",
                     "watch_until": "2026-10-18T14:30:00Z"})
    releases.append({**releases[0], "tag": "release-4", "run_id": "run_y", "status": "watching",
                     "watch_until": "2026-10-18T16:00:00Z"})   # still inside its window
    sb = report.summarize(report.load_audit(AUDIT), releases, now=NOW)["scoreboard"]
    assert sb["bad_changes_left"] == 1 and sb["releases_watching"] == 1


def test_render_html(tmp_path):
    out = report.render_html(AUDIT, RELEASES, tmp_path / "report.html", hmac_key=TEST_KEY, now=NOW)
    html = out.read_text()
    assert "chain verified" in html and "badge ok" in html
    assert "HMAC checked" in html
    assert "bad changes left on the server" in html
    for run_id in ("run_cart01", "run_stock02", "run_orders03"):
        assert f'id="run-{run_id}"' in html
    assert "if len(items) &lt;= 0 and cart_id != &#34;&lt;none&gt;&#34;:" in html   # diff escaped
    assert "EUR &amp; co" in html
    assert "<none>" not in html
    assert "<script" not in html and "http://" not in html and "https://" not in html


def test_tampering_flips_badge_at_right_seq(tmp_path):
    audit = tmp_path / "audit.jsonl"
    shutil.copy(AUDIT, audit)
    lines = audit.read_text().splitlines()
    entry = json.loads(lines[13])                    # seq 14: run_stock02 investigate (unsure)
    assert entry["seq"] == 14 and entry["stage"] == "investigate"
    entry["status"] = "pass"
    lines[13] = json.dumps(entry, sort_keys=True)
    audit.write_text("\n".join(lines) + "\n")
    assert report.verify_chain(report.load_audit(audit), TEST_KEY) == (False, 14)
    html = report.render_html(audit, RELEASES, tmp_path / "report.html", hmac_key=TEST_KEY, now=NOW).read_text()
    assert "chain verification failed at seq 14" in html and "badge bad" in html


def test_cli_prints_path(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AUDIT_HMAC_KEY", raising=False)
    out = tmp_path / "r.html"
    assert report.main(["--audit", str(AUDIT), "--releases", str(RELEASES), "--out", str(out)]) == 0
    assert capsys.readouterr().out.strip() == str(out)
    assert "chain verified" in out.read_text()


def test_render_live_marks():
    s = sample_summary()
    console = Console(record=True, width=120)
    console.print(report.render_live(s, {"error_rate_percent": 0.4, "p95_latency_ms": 42}))
    text = console.export_text()
    assert "error rate 0.4%" in text and "p95 42 ms" in text
    assert "run_orders03" in text and "current stage: escalate" in text
    assert "✓" in text and "✗" in text and "release-2" in text
    assert "bad changes left on the server: 0" in text
    # demo 2 as the current run shows the unsure mark
    stock = {**s, "runs": [r for r in s["runs"] if r["run_id"] == "run_stock02"]}
    console = Console(record=True, width=120)
    console.print(report.render_live(stock, {}))
    text = console.export_text()
    assert "⚠" in text and "replay failed 1/3" in text
