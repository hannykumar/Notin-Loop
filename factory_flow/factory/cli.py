"""Command-line entry point (PRD T1, §7.3). Run from factory_flow/:  python -m factory.cli <command>

  start [--fixer llm|scripted] [--no-traffic]   stable + traffic + agent loop with the rich live view
  break <cart|stock|404|normal>                  switch the traffic generator's request mix
  inbox | rollback <release-N> | replay <run_id> | pause | resume | report
  run-all [--fixer llm|scripted]                 reset, play demos 1 -> 2 -> 3, print the scoreboard

Threads (start / run-all): the watcher thread only scrapes and hands alerts to a queue.Queue; pipeline.run
always runs on another thread (main thread for start, a worker for run-all), because canary and the watch
window need the watcher ticking while they wait.

Patterned on SRE-agent/evaluation/ (run_all-style harness: play recorded scenarios, compare the outcome with
the expected one, print a scoreboard, exit 0 only if every case matches). New code otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from factory import alerts, audit, inbox, llm, pipeline, policy, releases, workspace
from factory.fixers import LLMFixer, ScriptedFixer

FACTORY_FLOW_DIR = Path(__file__).resolve().parents[1]
TARGET_DIR = workspace.TARGET_DIR
PATCHES_DIR = TARGET_DIR / "patches"
SCENARIOS_PATH = FACTORY_FLOW_DIR / "scenarios.json"
RESET_SH = FACTORY_FLOW_DIR / "scripts" / "reset.sh"
STABLE_URL = f"http://127.0.0.1:{policy.STABLE_PORT}"
PROFILES = {"cart": "break_cart", "stock": "break_stock", "404": "break_404", "normal": "normal"}
# alert -> pipeline done: reproduce + fix (LLM) + oracle + canary + watch window + rollback settle, with slack
DEMO_TIMEOUT_S = 2 * policy.FIXER_TIMEOUT_S + policy.CANARY_WINDOW_S + policy.WATCH_WINDOW_S + 300
# after a run: traffic is normal again; wait until the alert window holds only normal traffic before done()
SETTLE_S = policy.ALERT_WINDOW_S + policy.SCRAPE_INTERVAL_S * policy.ALERT_CONSECUTIVE_WINDOWS


def pids_path() -> Path:
    return audit.runs_dir() / "pids.json"


# ---------------------------------------------------------------- coupling to the core modules

_FALLBACK_WATCHER: Any = None


def shared_watcher():
    """The one Watcher the watcher thread ticks and every stage reads (watcher.default() singleton)."""
    global _FALLBACK_WATCHER
    from factory import watcher
    if hasattr(watcher, "default"):
        return watcher.default()
    if _FALLBACK_WATCHER is None:  # until watcher.default() lands
        _FALLBACK_WATCHER = watcher.Watcher()
    return _FALLBACK_WATCHER


def build_deps(fixer_mode: str = "llm") -> dict:
    """deps for pipeline.run; resolve() hands each stage only the keys its signature names."""
    import httpx
    return {
        "watcher": shared_watcher(),                         # triage/investigate/canary/promote/watch/rollback
        "workspace": workspace,                              # qualify, reproduce, fix
        "http_client": httpx.Client(base_url=STABLE_URL, timeout=5),   # investigate's safe replay vs stable
        "releases_path": releases.LEDGER_PATH,               # triage, investigate
        "fixer": RoutingFixer(fixer_mode),                   # fix
    }


# ---------------------------------------------------------------- fixer selection (PRD 8.3)

def llm_ready() -> bool:
    return os.environ.get("LLM_OFFLINE") != "1" and all(llm._config())


class RoutingFixer:
    """Picks the fixer from the bug report at fix() time; `name` is the fixer that ran.

    app/orders.py -> ScriptedFixer(orders_helper_fix.diff), deliberately (demo 3: the fix that later breaks
    /reports/daily); else LLMFixer if --fixer llm and the LLM is configured; the cart bug falls back to
    ScriptedFixer(cart_fix.diff) when the LLM is offline or unavailable.
    """

    def __init__(self, mode: str = "llm", *, patches_dir: Path = PATCHES_DIR,
                 llm_ready: Callable[[], bool] = llm_ready, llm_fixer: Callable[[], Any] = LLMFixer):
        self.mode, self.patches_dir, self.llm_ready, self.llm_fixer = mode, Path(patches_dir), llm_ready, llm_fixer
        self.current: Any = None

    @property
    def name(self) -> str:
        return getattr(self.current, "name", "routing")

    def _scripted(self, patch: str) -> ScriptedFixer:
        return ScriptedFixer(self.patches_dir / patch)

    def fix(self, workspace: Path, bug, failing_test: Path, timeout_s: int) -> str:
        files = list(bug.files or [])
        cart = "app/cart.py" in files
        if "app/orders.py" in files:
            self.current = self._scripted("orders_helper_fix.diff")
        elif self.mode == "llm" and self.llm_ready():
            self.current = self.llm_fixer()
            try:
                return self.current.fix(workspace, bug, failing_test, timeout_s)
            except llm.LLMUnavailable:
                if not cart:
                    raise
                self.current = self._scripted("cart_fix.diff")   # PRD 10: LLM down -> scripted patch
        elif cart:
            self.current = self._scripted("cart_fix.diff")
        else:
            raise ValueError(f"no fixer for {files}: LLM not used and no scripted patch")
        return self.current.fix(workspace, bug, failing_test, timeout_s)


# ---------------------------------------------------------------- agent loop

def consume(q: "queue.Queue", intake, make_deps: Callable[[], dict], stop: threading.Event, *,
            run: Callable[..., Any] = pipeline.run, after_run: Optional[Callable[[Any], None]] = None,
            results: Optional[list] = None) -> None:
    """Take alerts off the queue and run the line for each; intake.done() always follows."""
    while not stop.is_set():
        try:
            alert = q.get(timeout=0.5)
        except queue.Empty:
            continue
        deps = make_deps()
        try:
            state = run(alert, deps=deps)
            if results is not None:
                results.append(state)
            if after_run:
                after_run(state)
        except Exception as exc:  # noqa: BLE001 - one bad run must not stop the agent
            print(f"run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        finally:
            client = deps.get("http_client")
            if hasattr(client, "close"):
                client.close()
            intake.done(alert["fingerprint"])


def set_profile(target: str) -> dict:
    from factory.stages.canary import set_traffic
    return set_traffic(profile=PROFILES[target])


def ensure_repo() -> None:
    if not (TARGET_DIR / ".git").exists():
        subprocess.run([str(TARGET_DIR / "scripts" / "init_repo.sh")], check=True)


class Agent:
    """stable on prod + traffic generator + watcher thread; everything stopped in stop()."""

    def __init__(self, traffic: bool = True):
        self.traffic = traffic
        self.stop_event = threading.Event()
        self.q: queue.Queue = queue.Queue()
        self.intake = alerts.AlertIntake(self.q.put)
        self.watcher = shared_watcher()
        self.gen: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None

    def __enter__(self) -> "Agent":
        try:
            self.start()
        except BaseException:
            self.stop()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def _write_pids(self) -> None:
        pids = {"agent": os.getpid(), "stable": releases.stable_state().get("pid"),
                "traffic": self.gen.pid if self.gen else None}
        pids_path().write_text(json.dumps(pids, indent=2))

    def start(self) -> None:
        ensure_repo()
        from factory.stages.canary import set_traffic
        set_traffic(profile="normal", canary_url="", canary_share=policy.CANARY_TRAFFIC_SHARE)
        print("stable:", releases.deploy_stable("prod"))
        if self.traffic:
            out = open(audit.runs_dir() / "traffic.out", "w")
            self.gen = subprocess.Popen([sys.executable, "-m", "traffic.generate", "--stable", STABLE_URL],
                                        cwd=str(TARGET_DIR), stdout=out, stderr=subprocess.STDOUT)
            out.close()   # the child keeps its own handle
        self._write_pids()
        self.thread = threading.Thread(target=self.watcher.run_forever, args=(self.stop_event, self.intake.handle),
                                       name="watcher", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        try:
            if self.thread:
                self.thread.join(timeout=policy.SCRAPE_INTERVAL_S + 5)
        finally:
            try:
                if self.gen and self.gen.poll() is None:
                    self.gen.terminate()
                    try:
                        self.gen.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.gen.kill()
            finally:
                try:
                    from factory.stages.canary import set_traffic
                    set_traffic(canary_url="")
                finally:
                    try:
                        workspace.cleanup()          # canary + worktrees this process created
                    finally:
                        releases.stop_stable()
                        pids_path().unlink(missing_ok=True)

    def health(self) -> dict:
        try:
            return dict(self.watcher.current("*", "stable").get("metrics") or {})
        except Exception:  # noqa: BLE001 - the view must never crash the loop
            return {}

    def frame(self):
        from factory import report
        return report.render_live(report.summarize(report.load_audit(), report.load_releases()), self.health())


# ---------------------------------------------------------------- scoreboard

def _alert_path(alert: dict) -> str:
    return (alert.get("labels") or {}).get("path") or (alert.get("breach") or {}).get("route") or ""


def score(scenarios: dict, entries: list[dict], ledger: list[dict], cases: list[dict], final: dict
          ) -> tuple[list[dict], int, bool]:
    """(rows, bad_changes_left, all_match) from the audit log, releases.json, inbox cases and the live end state."""
    rows: list[dict] = []
    for sc in scenarios["scenarios"]:
        exp = sc["expected"]
        run_id = next((e["run_id"] for e in entries if e.get("stage") == "run_start"
                       and _alert_path(e.get("alert") or {}) == sc["alert_path"]), None)
        end = next((e for e in entries if e.get("stage") == "run_end" and e.get("run_id") == run_id), {})
        case = next((c for c in cases if c.get("run_id") == run_id and c.get("stage")), {})
        rel = next((r for r in ledger if run_id and r.get("run_id") == run_id), {})
        actual = {"outcome": end.get("outcome", "no run"), "stage": case.get("stage"),
                  "release": rel.get("tag"), "release_status": rel.get("status")}
        actual = {k: actual[k] for k in exp}
        rows.append({"id": sc["id"], "run_id": run_id, "expected": exp, "actual": actual, "match": actual == exp})

    want = scenarios["final_state"]
    head = final.get("prod_head")
    bad = sum(1 for r in ledger if r.get("commit") == head and r.get("status") != "kept")
    bad += final.get("prod_release") != want["prod_release"]
    got = {k: final.get(k) for k in want if k != "bad_changes_left"}
    exp_final = {k: v for k, v in want.items() if k != "bad_changes_left"}
    rows.append({"id": "final", "run_id": None, "expected": exp_final, "actual": got, "match": got == exp_final})
    return rows, bad, all(r["match"] for r in rows) and bad == want["bad_changes_left"]


def final_state() -> dict:
    import httpx
    head = releases.rev("prod")
    tags = [t for t in releases.git("tag", "--points-at", head).splitlines() if t.startswith("release-")]
    try:
        healthy = httpx.get(STABLE_URL + "/", timeout=3).status_code == 200
    except httpx.HTTPError:
        healthy = False
    return {"prod_head": head, "prod_release": max(tags, key=releases.tag_number) if tags else None,
            "stable_ref": releases.stable_state().get("ref"), "stable_healthy": healthy,
            "line_paused": pipeline.is_paused()}


def print_scoreboard(rows: list[dict], bad: int) -> None:
    print("\nScoreboard")
    for r in rows:
        print(f"  [{'ok' if r['match'] else 'MISMATCH'}] {r['id']:<6} {r['run_id'] or '':<14} "
              f"expected {json.dumps(r['expected'])}  actual {json.dumps(r['actual'])}")
    print(f"bad changes left on the server: {bad}")


# ---------------------------------------------------------------- commands

def cmd_start(args) -> int:
    from rich.live import Live
    try:
        with Agent(traffic=not args.no_traffic) as agent, \
                Live(get_renderable=agent.frame, refresh_per_second=2):
            consume(agent.q, agent.intake, lambda: build_deps(args.fixer), agent.stop_event)
    except KeyboardInterrupt:
        print("stopped")
    return 0


def _wait_quiet() -> None:
    """Triage treats any release (or prod commit) younger than ROLLBACK_LOOKBACK_S as the alert's cause;
    start a break only after the previous change is out of that window."""
    while True:
        last = float(releases.git("log", "-1", "--format=%ct", "prod"))
        left = last + policy.ROLLBACK_LOOKBACK_S + policy.SCRAPE_INTERVAL_S - time.time()
        if releases.in_window() is None and left <= 0:
            return
        print(f"  waiting {max(left, policy.SCRAPE_INTERVAL_S):.0f}s for the triage lookback window", flush=True)
        time.sleep(max(min(left, 30), policy.SCRAPE_INTERVAL_S))


def _wait_run(results: list, start: int, path: str, timeout_s: float):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for state in results[start:]:
            if _alert_path(state.alert) == path:
                return state
        time.sleep(0.5)
    return None


def cmd_run_all(args) -> int:
    subprocess.run([str(RESET_SH)], check=True)
    scenarios = json.loads(SCENARIOS_PATH.read_text())
    results: list = []
    with Agent() as agent:
        worker = threading.Thread(
            target=consume, args=(agent.q, agent.intake, lambda: build_deps(args.fixer), agent.stop_event),
            kwargs={"results": results, "after_run": lambda _s: time.sleep(SETTLE_S)}, name="runner", daemon=True)
        worker.start()
        for sc in scenarios["scenarios"]:
            if pipeline.is_paused():
                print("  line paused by an earlier demo; resuming for the next one")
                pipeline.resume(actor="run-all")
            _wait_quiet()
            print(f"== {sc['id']}: break {sc['break']} ({sc['name']})", flush=True)
            start = len(results)
            set_profile(sc["break"])
            state = _wait_run(results, start, sc["alert_path"], DEMO_TIMEOUT_S)
            set_profile("normal")
            ended = audit.read(state.run_id)[-1].get("outcome") if state else "TIMEOUT"
            print(f"   {state.run_id if state else '-'}: {ended}", flush=True)
        final = final_state()
        agent.stop_event.set()
        worker.join(timeout=SETTLE_S + 5)
    rows, bad, ok = score(scenarios, audit.read(), [r.__dict__ for r in releases.load()], inbox.list_cases(), final)
    print_scoreboard(rows, bad)
    return 0 if ok else 1


def cmd_break(args) -> int:
    print(json.dumps(set_profile(args.target), sort_keys=True))
    return 0


def cmd_inbox(args) -> int:
    cases = inbox.list_cases()
    for c in cases:
        print(f"{c.get('run_id') or '-':<26} {c.get('severity', ''):<8} {c.get('stage') or c.get('kind') or '':<12} "
              f"{c.get('reason') or c.get('summary') or ''}")
    if not cases:
        print("inbox empty")
    return 0


def cmd_rollback(args) -> int:
    from factory.stages import rollback
    w, stop = shared_watcher(), threading.Event()
    # health verification reads the watcher, so it must tick in this process too
    threading.Thread(target=w.run_forever, args=(stop, lambda _b: None), daemon=True).start()
    try:
        v = rollback.rollback(args.tag, actor="human", reason="manual rollback (cli)", watcher=w,
                              audit_append=audit.append)
    finally:
        stop.set()
    print(json.dumps({"status": v.status, "reasons": v.reasons, "deployed_ref": v.evidence.get("deployed_ref"),
                      "inbox_case": v.evidence.get("inbox_case")}, indent=2, default=str))
    return 0 if v.status == "pass" else 1


def cmd_replay(args) -> int:
    from factory import replay
    return replay.main([args.run_id])


def cmd_pause(args) -> int:
    pipeline.pause(actor="human", reason="cli pause")
    print("line paused")
    return 0


def cmd_resume(args) -> int:
    pipeline.resume(actor="human")
    print("line resumed")
    return 0


def cmd_report(args) -> int:
    from factory import report
    print(report.render_html())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m factory.cli", description="SRE dark factory")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("start", help="stable + traffic + agent loop (Ctrl-C stops everything)")
    s.add_argument("--fixer", choices=["llm", "scripted"], default="llm")
    s.add_argument("--no-traffic", action="store_true")
    s.set_defaults(fn=cmd_start)
    s = sub.add_parser("run-all", help="reset, play demos 1 -> 2 -> 3, print the scoreboard")
    s.add_argument("--fixer", choices=["llm", "scripted"], default="llm")
    s.set_defaults(fn=cmd_run_all)
    s = sub.add_parser("break", help="switch the traffic generator profile")
    s.add_argument("target", choices=list(PROFILES))
    s.set_defaults(fn=cmd_break)
    s = sub.add_parser("rollback", help="manual rollback of a release (actor human)")
    s.add_argument("tag")
    s.set_defaults(fn=cmd_rollback)
    s = sub.add_parser("replay", help="verify the audit chain and re-derive a run's decision")
    s.add_argument("run_id")
    s.set_defaults(fn=cmd_replay)
    for name, fn in (("inbox", cmd_inbox), ("pause", cmd_pause), ("resume", cmd_resume), ("report", cmd_report)):
        sub.add_parser(name).set_defaults(fn=fn)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
