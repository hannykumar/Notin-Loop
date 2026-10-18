"""rich live view and HTML report (PRD B8).

Reads runs/audit.jsonl (PRD 7.7 entries) and runs/releases.json ({"releases": [Release, ...]}) and turns
them into a per-run summary for people: stages with pass / fail / unsure and reasons, the final outcome,
the diff, a timeline, and a scoreboard.

# Patterned on SRE-agent/agent/run_summaries.py @ b4f7e0c: small pure helpers that turn raw run state
# into a plain-language dict (headline, outcome, evidence, next step), rendered separately.

Scoreboard definition of "bad changes left on the server": releases that are neither "kept" nor
"rolled_back" although their watch window (watch_until) has already passed, i.e. an agent change that
is still live without a clean verdict. A release still inside its window is counted as "watching", not bad.

Usage:  python -m factory.report [--audit P] [--releases P] [--out P]
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from factory import audit
from factory.contracts import RELEASES_FILE_KEY
from factory.sre_base import FACTORY_FLOW_DIR
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# PRD 6 stage order. Anything else in the log (alert, escalate, inbox, run_start) is a meta event:
# shown in the timeline, not as a stage mark, and not used to decide the outcome.
STAGES = ["triage", "investigate", "qualify", "reproduce", "fix", "inspect_diff", "oracle", "canary",
          "promote", "watch", "rollback"]
MARKS = {"pass": "✓", "fail": "✗", "unsure": "⚠"}
MARK_STYLE = {"pass": "green", "fail": "red", "unsure": "yellow"}
OUTCOME_STYLE = {"kept": "green", "promoted": "cyan", "escalated": "yellow", "rolled_back": "magenta",
                 "rollback_failed": "red", "in_progress": "white"}


# --- Readers -----------------------------------------------------------------------

def load_audit(path: Path | str | None = None) -> list[dict]:
    path = Path(path or audit.audit_path())
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                entries.append({"_unparseable": line})
    return entries


def load_releases(path: Path | str | None = None) -> list[dict]:
    path = Path(path or audit.runs_dir() / "releases.json")
    if not path.exists():
        return []
    return list(json.loads(path.read_text()).get(RELEASES_FILE_KEY, []))


# --- Summary -----------------------------------------------------------------------

def _parse_ts(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_diff(evidence: dict) -> str:
    if evidence.get("diff"):
        return str(evidence["diff"])
    path = evidence.get("diff_path")
    if path:
        candidate = Path(path) if Path(path).is_absolute() else FACTORY_FLOW_DIR / path
        if candidate.exists():
            return candidate.read_text()
    return ""


def _outcome(stages: list[dict], release: Optional[dict]) -> str:
    by_name = {s["stage"]: s for s in stages}       # last entry per stage wins
    if "rollback" in by_name:
        return "rolled_back" if by_name["rollback"]["status"] == "pass" else "rollback_failed"
    if release and release.get("status") in ("kept", "rolled_back"):
        return str(release["status"])
    if by_name.get("watch", {}).get("status") == "pass":
        return "kept"
    if any(s["status"] != "pass" for s in stages):
        return "escalated"
    if by_name.get("promote", {}).get("status") == "pass":
        return "promoted"
    return "in_progress"


def summarize(entries: list[dict], releases: list[dict], now: Optional[datetime] = None) -> dict:
    """Per-run summary and scoreboard. `now` decides which watch windows have passed (default: UTC now)."""
    now = now or datetime.now(timezone.utc)
    releases_by_run = {}
    for rel in releases:
        releases_by_run.setdefault(rel.get("run_id"), []).append(rel)

    runs: dict[str, dict] = {}
    for entry in entries:
        if "_unparseable" in entry:
            continue
        run_id = str(entry.get("run_id", "-"))
        run = runs.setdefault(run_id, {"run_id": run_id, "stages": [], "timeline": [], "diff": "", "alert": {},
                                       "started_at": entry.get("ts", ""), "ended_at": "", "inbox_case": ""})
        evidence = dict(entry.get("evidence") or {})
        item = {
            "seq": entry.get("seq"), "ts": entry.get("ts", ""), "stage": entry.get("stage", "?"),
            "actor": entry.get("actor", "agent"), "status": entry.get("status", "unsure"),
            "reasons": list(entry.get("reasons") or []), "checks": dict(entry.get("checks") or {}),
            "evidence": {k: v for k, v in evidence.items() if k != "diff"}, "route": entry.get("route"),
        }
        item["mark"] = MARKS.get(item["status"], "?")
        run["timeline"].append(item)
        run["ended_at"] = item["ts"]
        if item["stage"] in STAGES:
            run["stages"].append(item)
        elif item["stage"] in ("alert", "run_start"):
            run["alert"] = evidence
        if evidence.get("inbox_case"):
            run["inbox_case"] = evidence["inbox_case"]
        diff = _load_diff(evidence)
        if diff:
            run["diff"] = diff

    for run in runs.values():
        rels = releases_by_run.get(run["run_id"], [])
        run["releases"] = rels
        run["outcome"] = _outcome(run["stages"], rels[-1] if rels else None)
        stopped = next((s for s in run["stages"] if s["status"] != "pass" and s["stage"] != "rollback"), None)
        run["stopped_at"] = stopped["stage"] if stopped else ""
        run["headline"] = f"{run['run_id']}: {run['alert'].get('path', 'unknown path')} -> {run['outcome']}"

    release_rows = []
    bad = watching = 0
    for rel in releases:
        until = _parse_ts(rel.get("watch_until", ""))
        status = rel.get("status", "")
        is_bad = status not in ("kept", "rolled_back") and until is not None and until < now
        bad += is_bad
        watching += status == "watching" and not is_bad
        release_rows.append({**rel, "bad": is_bad})

    outcomes = [r["outcome"] for r in runs.values()]
    scoreboard = {
        "runs": len(runs),
        "promoted_kept": outcomes.count("kept"),
        "promoted_watching": outcomes.count("promoted"),
        "escalated": outcomes.count("escalated"),
        "rolled_back": outcomes.count("rolled_back") + outcomes.count("rollback_failed"),
        "releases": len(releases),
        "releases_watching": watching,
        "bad_changes_left": bad,
    }
    return {"runs": list(runs.values()), "releases": release_rows, "scoreboard": scoreboard}


# --- rich live view ----------------------------------------------------------------

def render_live(summary: dict, health: Optional[dict] = None):
    """One rich renderable: health line, current run and stage, a mark per stage, releases, scoreboard."""
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    health = health or {}
    err, p95 = health.get("error_rate_percent"), health.get("p95_latency_ms")
    health_line = Text("Server health  ", style="bold")
    health_line.append(f"error rate {err:.1f}%" if isinstance(err, (int, float)) else "error rate n/a",
                       style="red" if isinstance(err, (int, float)) and err >= 5 else "green")
    health_line.append("   ")
    health_line.append(f"p95 {p95:.0f} ms" if isinstance(p95, (int, float)) else "p95 n/a")

    parts: list[Any] = [health_line]
    runs = summary.get("runs") or []
    if runs:
        run = runs[-1]
        current = run["timeline"][-1]["stage"] if run["timeline"] else "-"
        head = Text(f"Run {run['run_id']}   current stage: {current}   outcome: ", style="bold")
        head.append(run["outcome"], style=OUTCOME_STYLE.get(run["outcome"], "white"))
        parts.append(head)
        table = Table(show_header=True, header_style="bold", expand=True)
        table.add_column("", width=2)
        table.add_column("stage")
        table.add_column("reason")
        for s in run["stages"]:
            table.add_row(Text(s["mark"], style=MARK_STYLE.get(s["status"], "white")), s["stage"],
                          "; ".join(s["reasons"]) or s["status"])
        parts.append(table)
    else:
        parts.append(Text("No runs yet.", style="dim"))

    rel_table = Table(title="Releases", show_header=True, header_style="bold", expand=True)
    for col in ("tag", "run", "status", "watch until"):
        rel_table.add_column(col)
    for rel in summary.get("releases") or []:
        style = "red" if rel.get("bad") else {"kept": "green", "rolled_back": "magenta"}.get(rel.get("status"), "cyan")
        rel_table.add_row(rel.get("tag", ""), rel.get("run_id", ""), Text(rel.get("status", ""), style=style),
                          rel.get("watch_until", ""))
    parts.append(rel_table)

    sb = summary.get("scoreboard") or {}
    parts.append(Text(
        f"runs {sb.get('runs', 0)} | kept {sb.get('promoted_kept', 0)} | escalated {sb.get('escalated', 0)} | "
        f"rolled back {sb.get('rolled_back', 0)} | bad changes left on the server: {sb.get('bad_changes_left', 0)}",
        style="bold"))
    return Panel(Group(*parts), title="SRE Dark Factory", border_style="blue")


def watch(audit_path: Path | str | None = None, releases_path: Path | str | None = None,
          health_fn: Callable[[], dict] = dict, refresh_s: float = 1.0) -> None:
    """Re-read the audit log and releases every refresh_s seconds and redraw (Ctrl+C to stop)."""
    import time

    from rich.live import Live

    def frame():
        return render_live(summarize(load_audit(audit_path), load_releases(releases_path)), health_fn())

    with Live(frame(), refresh_per_second=4, screen=False) as live:
        try:
            while True:
                time.sleep(refresh_s)
                live.update(frame())
        except KeyboardInterrupt:
            pass


# --- HTML report -------------------------------------------------------------------

def render_html(audit_path: Path | str | None = None, releases_path: Path | str | None = None,
                out_path: Path | str | None = None, hmac_key: Optional[str] = None,
                now: Optional[datetime] = None) -> Path:
    """Write a self-contained report.html (inline CSS, no JS). The chain is verified with audit.verify, which
    needs a key (hmac_key or AUDIT_HMAC_KEY); without one the report says "not verified"."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    audit_path = Path(audit_path or audit.audit_path())
    releases_path = Path(releases_path or audit.runs_dir() / "releases.json")
    out_path = Path(out_path or audit.runs_dir() / "report.html")
    entries = load_audit(audit_path)
    releases = load_releases(releases_path)
    key = hmac_key if hmac_key is not None else os.environ.get("AUDIT_HMAC_KEY") or None
    ok, first_bad = audit.verify(audit_path, key) if key else (False, None)
    summary = summarize(entries, releases, now=now)
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=select_autoescape(["html"]))
    html = env.get_template("report.html").render(
        summary=summary, chain_ok=ok, first_bad_seq=first_bad, hmac_checked=bool(key), entries_count=len(entries),
        audit_path=str(audit_path), releases_path=str(releases_path), marks=MARKS,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m factory.report", description="Render the HTML report.")
    parser.add_argument("--audit")
    parser.add_argument("--releases")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    print(render_html(args.audit, args.releases, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
