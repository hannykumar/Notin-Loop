"""Browser dashboard for the demo: one page + a small JSON API over the same files the CLI uses.

    python -m factory.cli start --ui [PORT]      inside the agent process (shares the watcher; live health)
    python -m factory.dashboard [--port 8099]    standalone: state from runs/ files, buttons still work

Patterned on SRE-agent/runtime/api.py + SRE-agent/frontend/ (plain HTML/CSS/JS served by FastAPI, no build).
"""
from __future__ import annotations

import argparse
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from factory import audit, inbox, pipeline, report

STATIC = Path(__file__).resolve().parent / "static"


def _try(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 - every state field degrades to null, the page never 500s
        return default


def _traffic_profile() -> Optional[str]:
    from factory.stages.canary import _traffic
    return _traffic().read_control().get("profile")


def _chain_verified() -> Optional[bool]:
    return audit.verify()[0]   # raises without AUDIT_HMAC_KEY -> null ("not checked")


def _summary() -> dict:
    s = report.summarize(report.load_audit(), report.load_releases())
    # pause/resume entries have run_id "" and summarize groups them as a pseudo-run; not a run to show
    s["runs"] = [r for r in s["runs"] if r["run_id"] not in ("", "-")]
    return s


def state(agent: Any = None, rollbacks: Optional[dict] = None) -> dict:
    return {
        "summary": _try(_summary, {"runs": [], "releases": [], "scoreboard": {}}),
        "stages": pipeline.STAGE_ORDER + ["rollback"],
        "releases": _try(report.load_releases, []),
        "inbox": _try(inbox.list_cases, []),
        "paused": _try(pipeline.is_paused),
        "health": _try(agent.health) if agent is not None else None,
        "traffic_profile": _try(_traffic_profile),
        "chain_verified": _try(_chain_verified),
        "rollbacks": dict(rollbacks or {}),
    }


def create_app(agent: Any = None) -> FastAPI:
    from factory import cli

    app = FastAPI(title="SRE Dark Factory dashboard")
    rollbacks: dict[str, dict] = {}   # tag -> {"status": ...}; shown next to the release

    @app.exception_handler(Exception)
    async def _error(_req, exc: Exception):   # actions report their error as JSON instead of a bare 500 page
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500)

    @app.get("/")
    def page() -> FileResponse:
        return FileResponse(STATIC / "dashboard.html")

    @app.get("/api/state")
    def api_state() -> dict:
        return state(agent, rollbacks)

    @app.post("/api/break/{name}")
    def api_break(name: str) -> dict:
        if name not in cli.PROFILES:
            raise HTTPException(404, f"unknown profile {name}; one of {list(cli.PROFILES)}")
        return {"ok": True, "control": cli.set_profile(name)}

    @app.post("/api/pause")
    def api_pause() -> dict:
        pipeline.pause(actor="human", reason="dashboard pause")
        return {"ok": True, "paused": True}

    @app.post("/api/resume")
    def api_resume() -> dict:
        pipeline.resume(actor="human")
        return {"ok": True, "paused": False}

    @app.post("/api/rollback/{tag}")
    def api_rollback(tag: str) -> dict:
        if rollbacks.get(tag, {}).get("status") == "running":
            return {"ok": True, "accepted": False, "status": "running"}
        rollbacks[tag] = {"status": "running"}

        def work() -> None:   # same call as cli.cmd_rollback; takes the settle window, so off the request
            from factory.stages import rollback
            try:
                v = rollback.rollback(tag, actor="human", reason="manual rollback (dashboard)",
                                      watcher=cli.shared_watcher(), audit_append=audit.append)
                rollbacks[tag] = {"status": v.status, "reasons": v.reasons}
            except Exception as exc:  # noqa: BLE001
                rollbacks[tag] = {"status": "fail", "reasons": [f"{type(exc).__name__}: {exc}"]}

        threading.Thread(target=work, name=f"rollback-{tag}", daemon=True).start()
        return {"ok": True, "accepted": True, "status": "running"}

    @app.post("/api/replay/{run_id}")
    def api_replay(run_id: str) -> dict:
        from factory import replay
        result = replay.replay(run_id)
        return {"ok": True, "reproduced": result["reproduced"], "lines": replay.format_result(result)}

    @app.post("/api/report")
    def api_report() -> dict:
        return {"ok": True, "path": str(report.render_html()), "url": "/report"}

    @app.get("/report")
    def get_report() -> FileResponse:
        path = audit.runs_dir() / "report.html"
        if not path.exists():
            report.render_html(out_path=path)
        return FileResponse(path)

    return app


def serve_in_thread(agent: Any, port: int = 8099) -> threading.Thread:
    """uvicorn in a daemon thread of the calling process (cli start --ui), so it sees the agent's watcher."""
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(create_app(agent), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, name="dashboard", daemon=True)
    t.start()
    return t


def main(argv: Optional[list[str]] = None) -> int:
    import uvicorn
    p = argparse.ArgumentParser(prog="python -m factory.dashboard", description="Standalone dashboard (no agent).")
    p.add_argument("--port", type=int, default=8099)
    args = p.parse_args(argv)
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
