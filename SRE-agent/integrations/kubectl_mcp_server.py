"""integrations/kubectl_mcp_server.py

A small "MCP-style" HTTP server wrapping kubectl safely.

Why this exists
---------------
The agent should *not* run kubectl directly. Instead, it calls this server, and the server
applies safety rules:

- Hard timeout for kubectl calls
- Namespace allowlist / denylist (deny kube-system by default)
- Optional write actions (off by default)
- Optional approval token for writes (off by default)

Environment variables
---------------------
- MCP_HOST (default 127.0.0.1)
- MCP_PORT (default 8088)
- MCP_KUBECTL_TIMEOUT (default 10 seconds)
- MCP_NS_DENYLIST (default "kube-system,kube-public,kube-node-lease")
- MCP_NS_ALLOWLIST (default "", empty means allow all except denylist)
- MCP_ALLOW_WRITES (default 0)
- MCP_APPROVAL_TOKEN (default "", empty means no token required)

Run:
  python -m integrations.kubectl_mcp_server
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Dict, Optional, Set

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


app = FastAPI(title="kubectl-mcp-server")

HOST = os.getenv("MCP_HOST", "127.0.0.1")
PORT = int(os.getenv("MCP_PORT", "8088"))

DEFAULT_TIMEOUT_SEC = int(os.getenv("MCP_KUBECTL_TIMEOUT", "10"))

NAMESPACE_DENYLIST: Set[str] = set(
    [x.strip() for x in os.getenv("MCP_NS_DENYLIST", "kube-system,kube-public,kube-node-lease").split(",") if x.strip()]
)
NAMESPACE_ALLOWLIST_RAW = os.getenv("MCP_NS_ALLOWLIST", "")
NAMESPACE_ALLOWLIST: Set[str] = set([x.strip() for x in NAMESPACE_ALLOWLIST_RAW.split(",") if x.strip()])

ALLOW_WRITES = os.getenv("MCP_ALLOW_WRITES", "0").strip().lower() in ("1", "true", "yes")
APPROVAL_TOKEN = os.getenv("MCP_APPROVAL_TOKEN", "").strip()


# -------------------------
# Request models
# -------------------------
class BaseReq(BaseModel):
    namespace: str = Field(..., min_length=1, max_length=63, pattern=r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$", description="Kubernetes namespace")


class ServiceReq(BaseReq):
    service: str = Field(..., min_length=1, max_length=63, pattern=r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$", description="Service name (used as label app=<service>)")


class PodReq(BaseReq):
    pod_name: str = Field(..., min_length=1, max_length=253, pattern=r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$", description="Pod name")


class LogsReq(PodReq):
    lines: int = Field(200, ge=1, le=2000)
    since_time: str = ""


class ScaleReq(BaseReq):
    deployment: str = Field(..., min_length=1, max_length=253, pattern=r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")
    replicas: int = Field(..., ge=0, le=10)


# -------------------------
# Safety helpers
# -------------------------
def _ns_ok(namespace: str) -> bool:
    """Namespace policy gate (denylist + optional allowlist)."""
    ns = (namespace or "").strip()
    if not ns:
        return False
    if ns in NAMESPACE_DENYLIST:
        return False
    if NAMESPACE_ALLOWLIST and ns not in NAMESPACE_ALLOWLIST:
        return False
    return True


def _require_write(x_approval_token: Optional[str]) -> None:
    """Write gate (writes disabled by default + optional token requirement)."""
    if not ALLOW_WRITES:
        raise HTTPException(status_code=403, detail="Write actions disabled (set MCP_ALLOW_WRITES=1 to enable).")

    if APPROVAL_TOKEN and (x_approval_token or "").strip() != APPROVAL_TOKEN:
        raise HTTPException(status_code=403, detail="Missing/invalid approval token for write action.")


def _run_kubectl(args: list[str]) -> str:
    """Run kubectl safely (no shell, timeout, surface stderr)."""
    cmd = ["kubectl"] + args
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail=f"kubectl timed out after {DEFAULT_TIMEOUT_SEC}s")

    if completed.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"kubectl failed (code={completed.returncode}): {completed.stderr.strip() or completed.stdout.strip()}",
        )

    return completed.stdout


# -------------------------
# Read endpoints
# -------------------------
@app.post("/get_pod_status")
def get_pod_status(req: ServiceReq) -> Dict[str, Any]:
    if not _ns_ok(req.namespace):
        raise HTTPException(status_code=403, detail=f"Namespace blocked by policy: {req.namespace}")

    # Convention: pods are labeled app=<service>.
    out = _run_kubectl(["get", "pods", "-n", req.namespace, "-l", f"app={req.service}", "-o", "wide"])
    return {"raw": out}


@app.post("/describe_pod")
def describe_pod(req: PodReq) -> Dict[str, Any]:
    if not _ns_ok(req.namespace):
        raise HTTPException(status_code=403, detail=f"Namespace blocked by policy: {req.namespace}")

    out = _run_kubectl(["describe", "pod", req.pod_name, "-n", req.namespace])
    return {"raw": out}


@app.post("/get_pod_logs")
def get_pod_logs(req: LogsReq) -> Dict[str, Any]:
    if not _ns_ok(req.namespace):
        raise HTTPException(status_code=403, detail=f"Namespace blocked by policy: {req.namespace}")

    args = ["logs", req.pod_name, "-n", req.namespace, "--tail", str(req.lines)]
    if req.since_time.strip():
        args.extend(["--since-time", req.since_time.strip()])
    out = _run_kubectl(args)
    return {"lines": out.splitlines()}


@app.post("/get_events")
def get_events(req: BaseReq) -> Dict[str, Any]:
    if not _ns_ok(req.namespace):
        raise HTTPException(status_code=403, detail=f"Namespace blocked by policy: {req.namespace}")

    out = _run_kubectl(["get", "events", "-n", req.namespace, "--sort-by=.lastTimestamp"])
    return {"raw": out}


@app.post("/top_pod")
def top_pod(req: PodReq) -> Dict[str, Any]:
    if not _ns_ok(req.namespace):
        raise HTTPException(status_code=403, detail=f"Namespace blocked by policy: {req.namespace}")

    out = _run_kubectl(["top", "pod", req.pod_name, "-n", req.namespace])
    return {"raw": out}


# -------------------------
# Write endpoints (OFF by default)
# -------------------------
@app.post("/scale_deployment")
def scale_deployment(req: ScaleReq, x_approval_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    _require_write(x_approval_token)

    if not _ns_ok(req.namespace):
        raise HTTPException(status_code=403, detail=f"Namespace blocked by policy: {req.namespace}")

    out = _run_kubectl(["scale", "deployment", req.deployment, "-n", req.namespace, f"--replicas={req.replicas}"])
    return {"status": "ok", "raw": out}


@app.post("/restart_pod")
def restart_pod(req: ServiceReq, x_approval_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    """
    Restart pods by deleting pods for app=<service>.
    Kubernetes will recreate them if controlled by a Deployment/ReplicaSet.
    """
    _require_write(x_approval_token)

    if not _ns_ok(req.namespace):
        raise HTTPException(status_code=403, detail=f"Namespace blocked by policy: {req.namespace}")

    out = _run_kubectl(["delete", "pod", "-n", req.namespace, "-l", f"app={req.service}"])
    return {"status": "ok", "raw": out}


@app.post("/restart_coredns")
def restart_coredns(req: BaseReq, x_approval_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    """
    Special-case: restart CoreDNS.

    kube-system is denied by default. This endpoint allows kube-system explicitly,
    and only restarts the coredns deployment.
    """
    _require_write(x_approval_token)

    if (req.namespace or "").strip() != "kube-system":
        raise HTTPException(status_code=403, detail="restart_coredns is only allowed in kube-system")

    out = _run_kubectl(["rollout", "restart", "deployment/coredns", "-n", "kube-system"])
    return {"status": "ok", "raw": out}


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
