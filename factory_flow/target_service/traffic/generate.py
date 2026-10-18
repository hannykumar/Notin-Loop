"""Traffic generator for the target service.

Run:      python -m traffic.generate --stable http://127.0.0.1:8101 [--profile normal] [--duration 60]
Control:  python -m traffic.generate --set profile=break_cart
          python -m traffic.generate --set canary_url=http://127.0.0.1:8102 --set canary_share=0.2
          python -m traffic.generate --set canary_url=

The running generator re-reads the control file (default <factory_flow>/runs/traffic_control.json,
override with TRAFFIC_CONTROL) every second; its keys override the command-line values:
    profile        name of a JSON file in traffic/ (without .json)
    canary_url     base URL of the canary; empty or missing = no canary
    canary_share   fraction of requests sent to the canary (the 80/20 split is 0.2)
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

TRAFFIC_DIR = Path(__file__).resolve().parent
DEFAULT_CONTROL = Path(__file__).resolve().parents[2] / "runs" / "traffic_control.json"


def control_path() -> Path:
    return Path(os.getenv("TRAFFIC_CONTROL") or DEFAULT_CONTROL)


def read_control() -> dict[str, Any]:
    try:
        return json.loads(control_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def set_control(**updates: Any) -> dict[str, Any]:
    """Merge updates into the control file (atomic replace) and return the new contents."""
    control = {**read_control(), **updates}
    path = control_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(control, indent=2, sort_keys=True))
    tmp.replace(path)
    return control


def load_profile(name: str) -> dict[str, Any]:
    profile = json.loads((TRAFFIC_DIR / f"{name}.json").read_text())
    base_name = profile.get("base")
    if not base_name:
        return profile
    base = load_profile(base_name)
    return {
        "name": profile["name"],
        "rps": profile.get("rps", base.get("rps", 5)),
        "requests": base.get("requests", []) + profile.get("requests", []),
        "scheduled": base.get("scheduled", []) + profile.get("scheduled", []),
    }


def pick_target(stable_url: str, canary_url: str | None, canary_share: float) -> tuple[str, str]:
    if canary_url and random.random() < canary_share:
        return "canary", canary_url
    return "stable", stable_url


def send(http: httpx.Client, base_url: str, request: dict[str, Any]) -> str:
    try:
        response = http.request(
            request.get("method", "GET"),
            base_url.rstrip("/") + request["path"],
            params=request.get("query"),
            json=request.get("body"),
        )
        return str(response.status_code)
    except httpx.HTTPError:
        return "conn_error"


def run(stable_url: str, profile_name: str, canary_url: str | None, canary_share: float, duration: float | None) -> None:
    started = time.monotonic()
    last_scheduled: dict[str, float] = {}
    window: dict[str, Counter[str]] = {"stable": Counter(), "canary": Counter()}
    window_started = started
    profile = load_profile(profile_name)

    with httpx.Client(timeout=5.0) as http:
        while duration is None or time.monotonic() - started < duration:
            control = read_control()
            wanted = control.get("profile", profile_name)
            if wanted != profile["name"]:
                profile = load_profile(wanted)
            target_canary = control.get("canary_url", canary_url) or None
            share = float(control.get("canary_share", canary_share))

            now = time.monotonic()
            due = [
                item for item in profile.get("scheduled", [])
                if now - last_scheduled.setdefault(item["path"], started) >= item["every_s"]
            ]
            for item in due:
                last_scheduled[item["path"]] = now
            requests = profile["requests"]
            batch = due + random.choices(requests, weights=[r.get("weight", 1) for r in requests], k=1)
            for request in batch:
                # scheduled batch jobs (stable_only) run against the primary deployment; the canary only gets a
                # share of user traffic, which is why a canary can miss a rarely used path (demo 3)
                instance, base_url = (("stable", stable_url) if request.get("stable_only")
                                      else pick_target(stable_url, target_canary, share))
                window[instance][send(http, base_url, request)] += 1

            if now - window_started >= 1.0:
                stamp = time.strftime("%H:%M:%S")
                canary_part = dict(window["canary"]) if target_canary else "-"
                print(f"{stamp} profile={profile['name']} stable={dict(window['stable'])} canary={canary_part}", flush=True)
                window = {"stable": Counter(), "canary": Counter()}
                window_started = now
            time.sleep(1.0 / profile.get("rps", 5))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stable", default="http://127.0.0.1:8101")
    parser.add_argument("--profile", default="normal")
    parser.add_argument("--canary", default=None, help="canary base URL (enables split mode)")
    parser.add_argument("--canary-share", type=float, default=0.2)
    parser.add_argument("--duration", type=float, default=None, help="seconds; default runs forever")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="update the control file and exit")
    args = parser.parse_args(argv)

    if args.set:
        updates: dict[str, Any] = {}
        for item in args.set:
            key, _, value = item.partition("=")
            updates[key] = float(value) if key == "canary_share" else value
        print(json.dumps(set_control(**updates), sort_keys=True))
        return 0

    try:
        run(args.stable, args.profile, args.canary, args.canary_share, args.duration)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
