"""Release ledger and deployment model (PRD B5, §7.6).

Ledger
------
runs/releases.json in the contract format {"releases": [dataclasses.asdict(Release), ...]} in promotion
order. Writes are atomic (temp file + replace). Functions take an optional `path` (tests use tmp files).

Deployment model (used by promote and, in B6, rollback)
--------------------------------------------------------
- target_service/ is its own git repo; branch `prod` and tags `release-N` live there. The agent never
  edits target_service's main checkout: it only creates commits in throwaway worktrees and moves refs.
- stable (:8101, SRE_INSTANCE=stable) runs from ONE persistent detached worktree of target_service at
  runs/deploy/stable. deploy_stable(ref) stops stable, checks that worktree out at `ref` (a tag such as
  release-2, or a commit), cleans it, and starts uvicorn there. "Which code is live" is therefore always
  exactly one git ref, recorded in runs/deploy/stable.json ({"ref", "commit", "pid", "port", "started_at", "started_ts"}).
- stable is stopped via this process's workspace registry, else via the pid in runs/deploy/stable.json,
  so a stable started by another process (e.g. the CLI) can still be restarted.
- canary (:8102) runs from a fresh temp worktree of `prod` + the diff only for the canary window.
- promote commits the diff in a fresh worktree of `prod`, tags release-N, moves `prod` to it with
  `git update-ref refs/heads/prod <new> <old>` (compare-and-swap), then deploy_stable("release-N").
- rollback (B6): deploy_stable(previous_tag) for the latest release, or `git revert` in a fresh worktree
  of prod + tag + update-ref + deploy_stable for an older one.

Patterned on SRE-agent/runtime/storage.py @ b4f7e0c (record_rollback / update_latest_rollback_status:
append a record, then update its status in place) — here a JSON file instead of SQLite.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from factory import workspace
from factory.contracts import RELEASES_FILE_KEY, Release
from factory.policy import ROLLBACK_LOOKBACK_S, STABLE_PORT

INITIAL_TAG = "release-0"
_TAG_RE = re.compile(r"^release-(\d+)$")


# ---------------------------------------------------------------- paths (FACTORY_RUNS_DIR, read at call time)

def ledger_path() -> Path:
    return workspace.runs_dir() / "releases.json"


def _deploy_dir() -> Path:
    return workspace.runs_dir() / "deploy"


def __getattr__(name: str) -> Any:   # releases.LEDGER_PATH keeps working, resolved per access
    if name == "LEDGER_PATH":
        return ledger_path()
    raise AttributeError(name)


# ---------------------------------------------------------------- time helpers

def utc_iso(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(time.time() if ts is None else ts, tz=timezone.utc)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------- ledger

def _path(path: Optional[Path]) -> Path:
    return Path(path) if path is not None else ledger_path()


def _from_dict(raw: dict) -> Release:
    known = {f.name for f in fields(Release)}
    return Release(**{k: v for k, v in raw.items() if k in known})


def load(path: Optional[Path] = None) -> list[Release]:
    p = _path(path)
    if not p.exists():
        return []
    data = json.loads(p.read_text() or "{}")
    return [_from_dict(r) for r in data.get(RELEASES_FILE_KEY, [])]


def _save(releases: list[Release], path: Optional[Path] = None) -> None:
    p = _path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({RELEASES_FILE_KEY: [asdict(r) for r in releases]}, indent=2, sort_keys=True))
    tmp.replace(p)


def append(release: Release, path: Optional[Path] = None) -> Release:
    releases = load(path)
    if any(r.tag == release.tag for r in releases):
        raise ValueError(f"release {release.tag} already in the ledger")
    _save([*releases, release], path)
    return release


def update(tag: str, path: Optional[Path] = None, **changes: Any) -> Release:
    releases = load(path)
    for i, r in enumerate(releases):
        if r.tag == tag:
            releases[i] = _from_dict({**asdict(r), **changes})
            _save(releases, path)
            return releases[i]
    raise KeyError(f"release {tag} not in the ledger")


def get(tag: str, path: Optional[Path] = None) -> Optional[Release]:
    return next((r for r in load(path) if r.tag == tag), None)


def latest(path: Optional[Path] = None) -> Optional[Release]:
    releases = load(path)
    return releases[-1] if releases else None


def previous_tag(path: Optional[Path] = None) -> str:
    """Tag a new release builds on: latest release still kept/watching, else release-0."""
    for r in reversed(load(path)):
        if r.status in ("kept", "watching"):
            return r.tag
    return INITIAL_TAG


def tag_number(tag: str) -> int:
    m = _TAG_RE.match(tag or "")
    return int(m.group(1)) if m else -1


def next_tag(path: Optional[Path] = None, existing_tags: tuple[str, ...] | list[str] = ()) -> str:
    """release-(N+1) over the ledger and any existing git tags."""
    numbers = [tag_number(r.tag) for r in load(path)] + [tag_number(t) for t in existing_tags] + [0]
    return f"release-{max(numbers) + 1}"


def in_window(ts: Optional[float] = None, path: Optional[Path] = None) -> Optional[Release]:
    """Most recent release that is 'watching', or promoted within ROLLBACK_LOOKBACK_S before ts
    (and not rolled back)."""
    ts = time.time() if ts is None else ts
    for r in reversed(load(path)):
        if r.status == "rolled_back":
            continue
        if r.status == "watching":
            return r
        promoted = parse_iso(r.promoted_at)
        if 0 <= ts - promoted <= ROLLBACK_LOOKBACK_S:
            return r
    return None


def by_hash(diff_sha256: str, path: Optional[Path] = None) -> Optional[Release]:
    return next((r for r in load(path) if diff_sha256 and r.diff_sha256 == diff_sha256), None)


# ---------------------------------------------------------------- git helpers

def git(*args: str, cwd: Optional[Path] = None, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd or workspace.target_dir()), capture_output=True,
                          text=True, timeout=workspace.GIT_TIMEOUT_S)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def rev(ref: str) -> str:
    return git("rev-parse", "--verify", f"{ref}^{{commit}}")


def git_tags() -> list[str]:
    return [t for t in git("tag", "--list", "release-*").splitlines() if t]


# ---------------------------------------------------------------- deployment (stable)

def _read_state() -> dict:
    try:
        return json.loads((_deploy_dir() / "stable.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def ensure_stable_worktree() -> Path:
    """Persistent detached worktree of target_service at runs/deploy/stable."""
    wt = _deploy_dir() / "stable"
    wt.parent.mkdir(parents=True, exist_ok=True)
    if (wt / ".git").exists():
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                cwd=str(wt), capture_output=True, text=True)
        if common.returncode == 0 and Path(common.stdout.strip()).resolve() == (workspace.target_dir() / ".git").resolve():
            return wt
    # missing, or stale after init_repo.sh --force recreated target_service/.git
    if wt.exists():
        shutil.rmtree(wt)
    git("worktree", "prune", check=False)
    git("worktree", "add", "--detach", str(wt), INITIAL_TAG)
    return wt


def stop_stable(timeout_s: float = 10.0) -> bool:
    """Stop stable (own registry first, else the pid recorded in stable.json). True if it is down."""
    stopped = workspace.stop_instance("stable")
    if not stopped:
        pid = _read_state().get("pid")
        if pid:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    deadline = time.monotonic() + timeout_s
    while workspace.port_busy(STABLE_PORT) and time.monotonic() < deadline:
        time.sleep(0.1)
    return not workspace.port_busy(STABLE_PORT)


def deploy_stable(ref: str) -> dict:
    """(Re)start stable on STABLE_PORT from runs/deploy/stable checked out at ref. Raises on failure."""
    commit = rev(ref)
    wt = ensure_stable_worktree()
    if not stop_stable():
        raise RuntimeError(f"port {STABLE_PORT} still busy; cannot restart stable")
    git("checkout", "--force", "--detach", commit, cwd=wt)
    git("clean", "-fdx", "-q", cwd=wt)
    info = workspace.start_instance(wt, STABLE_PORT, "stable")
    now = time.time()
    state = {"ref": ref, "commit": commit, "pid": info["pid"], "port": STABLE_PORT,
             "started_at": utc_iso(now), "started_ts": now}
    (_deploy_dir() / "stable.json").write_text(json.dumps(state, indent=2))
    return state


def stable_state() -> dict:
    """What stable runs: {"ref", "commit", "pid", "port", "started_at", "started_ts"} (empty if never deployed)."""
    return _read_state()
