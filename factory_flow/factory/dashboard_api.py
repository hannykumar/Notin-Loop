"""The one dashboard: status stream, incident cards, evidence-grounded buddy, and the demo controls.

    python -m factory.cli start --ui [PORT]           # inside the agent process (default 8099; shares its watcher)
    python -m factory.dashboard_api [--port 8100]     # standalone: state from runs/ files, buttons still work
    app.include_router(dashboard_api.router)          # or mount the /api routes on another FastAPI app

Reads the agent's files (audit.jsonl, inbox/, releases.json, deploy/stable.json). Human actions, all through
the same calls as the CLI (actor "human"): POST /api/rollback/{tag}, /api/pause, /api/resume, /api/break/{name}
(traffic profile), /api/replay/{run_id}, /api/report (+ GET /report). The buddy only explains: it never decides
a verdict and sees only the run's recorded evidence and diff. Patterned on SRE-agent/agent/run_summaries.py (turn run state into a plain summary)
via factory/report.py; the live view follows report.render_live's per-stage marks.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from factory import audit, inbox, llm, pipeline, policy, releases, report
from factory.pipeline import STAGE_ORDER, is_paused
from factory.watcher import Watcher

STATIC_DIR = Path(__file__).with_name("static")
GATES = STAGE_ORDER + ["watch", "rollback"]
OUTCOME_STATUS = {"kept": "promoted", "rolled_back": "rolled_back", "escalated": "escalated", "paused": "escalated"}
STATUS_INTERVAL_S = 2.0
DIFF_PREVIEW_LINES = 10
EVIDENCE_LINES = 12
STARTED_AT = time.time()

BUDDY_SYSTEM = (
    "You are the on-call buddy for an autonomous SRE agent. Answer ONLY from the evidence and diff given "
    "below for one incident. If the answer is not in that evidence, say exactly that it is not in the "
    "collected evidence. Do not guess, do not invent metrics, files or causes, and do not give advice beyond "
    "what the evidence supports. The agent's verdicts were made by deterministic checks; explain them, never "
    "overrule them. Be brief: at most 6 sentences, plain text, no markdown."
)
BUDDY_SUMMARY_QUESTION = (
    "Summarise this incident for the human on call: what broke, what the agent did at each gate, "
    "and why it ended with its final status."
)

router = APIRouter(prefix="/api")
_watcher = Watcher()


def set_watcher(watcher: Any) -> None:
    """Inject a watcher (tests, or the agent's own instance when mounted in the CLI server)."""
    global _watcher
    _watcher = watcher


def _try(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 - every extra status field degrades to null; the stream never breaks
        return default


# --- reading the agent's files ------------------------------------------------

def _runs() -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for entry in audit.read():
        if entry.get("run_id"):
            grouped.setdefault(entry["run_id"], []).append(entry)
    return grouped


def _releases() -> list[dict]:
    path = audit.runs_dir() / "releases.json"
    return [asdict(r) for r in releases.load(path)] if path.exists() else []


def _stable_state() -> dict:
    path = audit.runs_dir() / "deploy" / "stable.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _evidence(entries: list[dict], stage: str) -> dict:
    for entry in reversed(entries):
        if entry.get("stage") == stage:
            return entry.get("evidence") or {}
    return {}


def _bug(entries: list[dict], run_id: str) -> dict:
    case = inbox.get(run_id) or {}
    return case.get("bug") or _evidence(entries, "investigate").get("bug_report") or {}


def _diff(entries: list[dict], run_id: str) -> str:
    """The run's diff: artifacts/<run_id>/fix.diff, else the release commit it produced."""
    path = audit.runs_dir() / "artifacts" / run_id / "fix.diff"
    if path.is_file():
        return path.read_text()
    commit = _evidence(entries, "promote").get("commit")
    if commit:
        try:
            return releases.git("show", "--format=", commit)
        except Exception:  # target_service reset since the run: no diff to show
            return ""
    return ""


def _short(value: Any, limit: int = 140) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, sort_keys=True)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _evidence_summary(entries: list[dict]) -> list[str]:
    lines: list[str] = []
    for entry in entries:
        stage = entry.get("stage")
        if stage in ("run_start", "run_end"):
            continue
        for reason in entry.get("reasons") or []:
            lines.append(f"{stage}: {_short(reason)}")
    return lines[:EVIDENCE_LINES]


def _card(run_id: str, entries: list[dict]) -> dict:
    gates = {name: "skipped" for name in GATES}
    reasons: dict[str, str] = {}
    for entry in entries:
        if entry.get("stage") in gates and entry.get("status"):
            gates[entry["stage"]] = entry["status"]
            reasons[entry["stage"]] = "; ".join(_short(r, 300) for r in entry.get("reasons") or [])
    end = next((e for e in reversed(entries) if e.get("stage") == "run_end"), None)
    status = OUTCOME_STATUS.get(end.get("outcome"), "escalated") if end else "running"
    bug = _bug(entries, run_id)
    diff = _diff(entries, run_id)
    return {
        "id": run_id,
        "bug_id": bug.get("id", ""),
        "signature": bug.get("signature", "") or "(no signature yet)",
        "status": status,
        "current_stage": None if end else entries[-1].get("stage"),
        "started_at": entries[0].get("ts"),
        "ended_at": end.get("ts") if end else None,
        "release_tag": _evidence(entries, "promote").get("tag", ""),
        "gate_results": gates,
        "gate_reasons": reasons,
        "evidence_summary": _evidence_summary(entries),
        "diff_preview": "\n".join(diff.splitlines()[:DIFF_PREVIEW_LINES]),
        "needs_attention": inbox.get(run_id) is not None,
    }


def incidents() -> list[dict]:
    """One card per run, newest first (by the run's first audit seq: ts has one-second resolution)."""
    runs = sorted(_runs().items(), key=lambda item: item[1][0].get("seq", 0), reverse=True)
    return [_card(run_id, entries) for run_id, entries in runs]


def server_health() -> dict:
    """Stable is healthy if its last scrape worked and no route is at or above the alert error rate."""
    # current() ticks only when the watcher is stale (Watcher._fresh). The agent's shared watcher is already
    # ticking; forcing a tick here would double-count its breach streaks and drop the breaches it returns.
    overall = (_watcher.current("*", "stable").get("metrics") or {})
    hist = _watcher.history.get("stable")
    live = {"error_rate_percent": overall.get("error_rate_percent"), "p95_latency_ms": overall.get("p95_latency_ms"),
            "alert_error_rate_percent": policy.ALERT_ERROR_RATE_PERCENT, "slo_p95_ms": policy.SLO_MAX_P95_MS}
    if not hist:
        error = (_watcher.scrape_errors.get("stable") or {}).get("error", "no scrape yet")
        return {"healthy": False, "reason": error, "worst_error_rate_percent": None, **live}
    worst = 0.0
    for route in hist[-1][1]:
        snap = _watcher.current(route, "stable")
        if snap["samples"]:
            worst = max(worst, snap["metrics"].get("error_rate_percent", 0.0))
    healthy = worst < policy.ALERT_ERROR_RATE_PERCENT
    return {"healthy": healthy, "reason": "" if healthy else f"5xx rate {worst}%", "worst_error_rate_percent": worst,
            **live}


def _traffic_profile() -> Optional[str]:
    from factory.stages.canary import _traffic
    return _traffic().read_control().get("profile")


def _chain_verified() -> Optional[bool]:
    return audit.verify()[0]   # raises without AUDIT_HMAC_KEY -> null ("not checked")


def _scoreboard() -> dict:
    return report.summarize(report.load_audit(), report.load_releases())["scoreboard"]


def status_snapshot() -> dict:
    cards = incidents()
    running = next((c for c in cards if c["status"] == "running"), None)
    attention = sum(1 for c in cards if c["needs_attention"]) + (1 if is_paused() else 0)
    rels = _releases()
    stable = _stable_state()
    health = _try(server_health, {"healthy": None, "reason": "health unavailable"})
    return {
        "server_healthy": health["healthy"],
        "health": health,
        "current_run": running["current_stage"] if running else None,
        "current_run_id": running["id"] if running else None,
        "buddy_state": "active" if running else ("attention" if attention else "idle"),
        "incident_count": attention,
        "paused": is_paused(),
        "latest_release": ({"tag": rels[-1]["tag"], "status": rels[-1]["status"]} if rels else None),
        "deployed_ref": stable.get("ref", "release-0"),
        "uptime_s": round(time.time() - stable.get("started_ts", STARTED_AT)),
        "card_status": {c["id"]: c["status"] for c in cards},
        "releases": rels,
        "traffic_profile": _try(_traffic_profile),
        "chain_verified": _try(_chain_verified),
        "scoreboard": _try(_scoreboard),
    }


# --- buddy -----------------------------------------------------------------------

def buddy_context(run_id: str) -> str:
    entries = audit.read(run_id)
    if not entries:
        raise KeyError(run_id)
    trail = [{k: e.get(k) for k in ("seq", "stage", "status", "reasons", "checks", "evidence", "route", "outcome")
              if e.get(k) not in (None, "", [], {})} for e in entries]
    return (
        f"INCIDENT {run_id}\n\nINBOX CASE:\n{json.dumps(inbox.get(run_id), default=str)[:6000]}\n\n"
        f"AUDIT TRAIL:\n{json.dumps(trail, default=str)[:12000]}\n\nDIFF:\n{_diff(entries, run_id)[:4000] or '(none)'}"
    )


def _fallback_answer(run_id: str) -> str:
    card = _card(run_id, audit.read(run_id))
    gates = ", ".join(f"{k} {v}" for k, v in card["gate_results"].items() if v != "skipped")
    return (f"[LLM unavailable: evidence only] {card['signature']} ended {card['status']}. Gates: {gates}. "
            + " ".join(card["evidence_summary"][:4]))


def ask_buddy(run_id: str, question: str = "") -> str:
    context = buddy_context(run_id)
    try:
        return llm.complete(BUDDY_SYSTEM, f"{context}\n\nQUESTION: {question.strip() or BUDDY_SUMMARY_QUESTION}")
    except llm.LLMUnavailable:
        return _fallback_answer(run_id)


def _chunks(text: str, size: int = 24) -> Iterator[str]:
    # llm.complete is not streaming; the finished answer is sent in small chunks so the panel fills progressively.
    for i in range(0, len(text), size):
        yield text[i:i + size]


# --- routes ------------------------------------------------------------------------

class BuddyRequest(BaseModel):
    incident_id: str
    question: str = ""


@router.get("/status")
async def status_stream(once: bool = False) -> StreamingResponse:
    async def events():
        while True:
            snapshot = await run_in_threadpool(status_snapshot)
            yield f"data: {json.dumps(snapshot, default=str)}\n\n"
            if once:
                return
            await asyncio.sleep(STATUS_INTERVAL_S)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/incidents")
def list_incidents() -> list[dict]:
    return incidents()


@router.get("/incidents/{run_id}/audit")
def incident_audit(run_id: str) -> list[dict]:
    entries = audit.read(run_id)
    if not entries:
        raise HTTPException(status_code=404, detail=f"no audit entries for {run_id}")
    return entries


@router.post("/buddy")
async def buddy(request: BuddyRequest) -> StreamingResponse:
    try:
        answer = await run_in_threadpool(ask_buddy, request.incident_id, request.question)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown incident {request.incident_id}")
    return StreamingResponse(_chunks(answer), media_type="text/plain; charset=utf-8")


@router.post("/rollback/{tag}")
async def manual_rollback(tag: str) -> dict:
    from factory.stages import rollback  # imported lazily: it pulls git and process control

    verdict = await run_in_threadpool(rollback.rollback, tag, actor="human", reason="manual rollback from dashboard",
                                      audit_append=audit.append)
    return json.loads(json.dumps(asdict(verdict), default=str))


@router.post("/break/{name}")
def break_traffic(name: str) -> dict:
    from factory import cli

    if name not in cli.PROFILES:
        raise HTTPException(status_code=404, detail=f"unknown profile {name}; one of {list(cli.PROFILES)}")
    return {"ok": True, "control": cli.set_profile(name)}


@router.post("/pause")
def pause_line() -> dict:
    pipeline.pause(actor="human", reason="dashboard pause")
    return {"ok": True, "paused": True}


@router.post("/resume")
def resume_line() -> dict:
    pipeline.resume(actor="human")
    return {"ok": True, "paused": False}


@router.post("/replay/{run_id}")
def replay_run(run_id: str) -> dict:
    from factory import replay

    result = replay.replay(run_id)
    return {"ok": True, "reproduced": result["reproduced"], "lines": replay.format_result(result)}


@router.post("/report")
def write_report() -> dict:
    return {"ok": True, "path": str(report.render_html()), "url": "/report"}


app = FastAPI(title="sre-dark-factory-dashboard")
app.include_router(router)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/report", include_in_schema=False)
def get_report() -> FileResponse:
    path = audit.runs_dir() / "report.html"
    if not path.exists():
        report.render_html(out_path=path)
    return FileResponse(path)


def serve_in_thread(agent: Any, port: int = 8099) -> threading.Thread:
    """uvicorn in a daemon thread of the agent process (cli start --ui): health comes from its shared watcher."""
    import uvicorn

    set_watcher(agent.watcher)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, name="dashboard", daemon=True)
    thread.start()
    return thread


def main(argv: Optional[list[str]] = None) -> None:
    import uvicorn

    parser = argparse.ArgumentParser(prog="python -m factory.dashboard_api", description="Standalone dashboard.")
    parser.add_argument("--port", type=int, default=8100)
    uvicorn.run(app, host="127.0.0.1", port=parser.parse_args(argv).port, log_level="warning")


if __name__ == "__main__":
    main()
