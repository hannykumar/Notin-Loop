"""Record the current runs/ as the read-only dashboard snapshot that the Vercel deploy serves.

    cd factory_flow && ../.venv/bin/python scripts/export_snapshot.py [--out DIR]   # default: demo_snapshot/

Reads runs/ (FACTORY_RUNS_DIR overrides) and writes nothing there itself (replay uses temp git worktrees of
target_service, as it always does). Copies audit.jsonl, releases.json, inbox/,
artifacts/, deploy/stable.json and traffic_control.json (TRAFFIC_CONTROL overrides); renders a fresh report.html
into the snapshot; replays every run (replay.replay) into replays.json; writes health.json from the live
dashboard (http://127.0.0.1:8099/api/status?once=true) or, if that does not answer, from the last
watch/rollback verdict; and meta.json {recorded_at, runs, note}.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

FACTORY_FLOW_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FACTORY_FLOW_DIR))

from factory import audit, policy, replay, report  # noqa: E402

LIVE_STATUS_URL = "http://127.0.0.1:8099/api/status?once=true"
NOTE = "Recorded from a live run of the agent. This copy is read-only; the live demo runs on the team's laptop."
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


def _live_health() -> dict | None:
    import httpx

    try:
        body = httpx.get(LIVE_STATUS_URL, timeout=5).text
        return json.loads(body.split("data: ", 1)[1])["health"]
    except Exception:  # noqa: BLE001 - no live dashboard: fall back to the recorded verdicts
        return None


def _default_health(entries: list[dict]) -> dict:
    """Healthy, with the metrics of the last watch/rollback verdict when it recorded any."""
    last = next((e for e in reversed(entries) if e.get("stage") in ("watch", "rollback")), {})
    evidence = last.get("evidence") or {}
    err = evidence.get("error_rate_percent")
    return {"healthy": True, "reason": f"recorded: last {last.get('stage', 'verdict')} {last.get('status', '')}".strip(),
            "worst_error_rate_percent": err, "error_rate_percent": err,
            "p95_latency_ms": evidence.get("p95_latency_ms"),
            "alert_error_rate_percent": policy.ALERT_ERROR_RATE_PERCENT, "slo_p95_ms": policy.SLO_MAX_P95_MS}


def _replays(run_ids: list[str]) -> dict:
    out = {}
    for run_id in run_ids:
        try:
            result = replay.replay(run_id)
            out[run_id] = {"reproduced": result["reproduced"], "lines": replay.format_result(result)}
        except Exception as exc:  # noqa: BLE001 - recorded as not reproduced, the export goes on
            out[run_id] = {"reproduced": False, "lines": [f"replay {run_id}", f"  - replay error: {exc}"[:300],
                                                          "decision NOT reproduced"]}
    return out


def export(out: Path) -> dict:
    src = audit.runs_dir()
    out = out.resolve()
    if out == src.resolve() or src.resolve() in out.parents:
        raise SystemExit(f"refusing to export into runs/ ({out})")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    for name in ("audit.jsonl", "releases.json", "deploy/stable.json"):
        if (src / name).is_file():
            (out / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / name, out / name)
    traffic = Path(os.environ.get("TRAFFIC_CONTROL") or src / "traffic_control.json")
    if traffic.is_file():
        shutil.copy2(traffic, out / "traffic_control.json")
    for name in ("inbox", "artifacts"):   # always present: the dashboard's readers must never need a mkdir
        if (src / name).is_dir():
            shutil.copytree(src / name, out / name, ignore=SKIP)
        else:
            (out / name).mkdir()
    report.render_html(audit_path=src / "audit.jsonl", releases_path=src / "releases.json",
                       out_path=out / "report.html")

    entries = audit.read()
    run_ids = list(dict.fromkeys(e["run_id"] for e in entries if e.get("run_id")))
    (out / "replays.json").write_text(json.dumps(_replays(run_ids), indent=2))
    health = _live_health() or _default_health(entries)
    (out / "health.json").write_text(json.dumps(health, indent=2, default=str))
    meta = {"recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "runs": len(run_ids),
            "note": NOTE}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=FACTORY_FLOW_DIR / "demo_snapshot")
    args = parser.parse_args(argv)
    meta = export(args.out)
    print(f"snapshot: {args.out} ({meta['runs']} runs, recorded {meta['recorded_at']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
