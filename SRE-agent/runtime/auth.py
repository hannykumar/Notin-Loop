from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from fastapi import Header, HTTPException

from runtime.settings import get_settings

ROLE_RANK = {
    "viewer": 10,
    "operator": 20,
    "approver": 30,
    "admin": 40,
}

ACTION_ALLOWLIST = {
    "approver": {"restart_pod", "scale_deployment", "restart_coredns", "gitops_scale_deployment"},
    "admin": {"*"},
}


@dataclass(frozen=True)
class Principal:
    token: str
    role: str
    name: str


def auth_enabled() -> bool:
    settings = get_settings()
    return bool(settings.api_tokens or settings.api_token)


def supported_roles() -> list[str]:
    return list(ROLE_RANK.keys())


def _parse_principals() -> Dict[str, Principal]:
    settings = get_settings()
    principals: Dict[str, Principal] = {}

    if settings.api_tokens:
        for raw_entry in settings.api_tokens.split(","):
            entry = raw_entry.strip()
            if not entry:
                continue
            parts = [part.strip() for part in entry.split("|")]
            if len(parts) < 2:
                continue
            token = parts[0]
            role = parts[1].lower()
            name = parts[2] if len(parts) > 2 and parts[2] else role
            if token and role in ROLE_RANK:
                principals[token] = Principal(token=token, role=role, name=name)

    if not principals and settings.api_token:
        principals[settings.api_token] = Principal(token=settings.api_token, role="admin", name="legacy-admin")

    return principals


def authenticate(x_api_token: Optional[str]) -> Principal:
    if not auth_enabled():
        return Principal(token="", role="admin", name="local-admin")
    token = (x_api_token or "").strip()
    principal = _parse_principals().get(token)
    if principal is None:
        raise HTTPException(status_code=401, detail="Missing or invalid ops API token")
    return principal


def require_role(min_role: str):
    if min_role not in ROLE_RANK:
        raise ValueError(f"Unsupported role: {min_role}")

    def dependency(x_api_token: Optional[str] = Header(default=None)) -> Principal:
        principal = authenticate(x_api_token)
        if ROLE_RANK[principal.role] < ROLE_RANK[min_role]:
            raise HTTPException(status_code=403, detail=f"Role '{principal.role}' cannot access this endpoint")
        return principal

    return dependency


def authorize_action(principal: Principal, action: dict) -> None:
    if principal.role == "admin" or not action:
        return

    allowed = ACTION_ALLOWLIST.get(principal.role, set())
    action_type = str(action.get("action_type", "")).strip()
    if "*" in allowed or action_type in allowed:
        return

    raise HTTPException(
        status_code=403,
        detail=f"Role '{principal.role}' cannot approve remediation action '{action_type or 'unknown'}'",
    )
