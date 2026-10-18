from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

CATALOG_PATH = Path("config/catalog/incident_catalog.json")
PRODUCTION_READ_TOOLS = [
    "get_deployment",
    "get_kubernetes_events",
    "query_prometheus_range",
    "query_loki_range",
    "get_git_diff",
    "get_service_dependencies",
    "search_runbooks",
    "search_previous_incidents",
]


@dataclass(frozen=True)
class IncidentDefinition:
    incident_type: str
    summary: str
    keywords: List[str]
    runbook_files: List[str]
    discriminating_tools: List[str]
    confirmation_rules: List[Dict[str, Any]]
    verification_rules: List[Dict[str, Any]]
    safe_actions: List[Dict[str, Any]]

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "IncidentDefinition":
        return cls(
            incident_type=str(payload.get("incident_type", "Unknown")),
            summary=str(payload.get("summary", "")),
            keywords=[str(item).lower() for item in payload.get("keywords", [])],
            runbook_files=[str(item) for item in payload.get("runbook_files", [])],
            discriminating_tools=[str(item) for item in payload.get("discriminating_tools", [])],
            confirmation_rules=list(payload.get("confirmation_rules", [])),
            verification_rules=list(payload.get("verification_rules", [])),
            safe_actions=list(payload.get("safe_actions", [])),
        )


@lru_cache(maxsize=1)
def load_catalog_payload() -> Dict[str, Any]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_incident_catalog() -> Dict[str, IncidentDefinition]:
    payload = load_catalog_payload()
    definitions = {}
    for item in payload.get("incident_types", []):
        definition = IncidentDefinition.from_payload(item)
        definitions[definition.incident_type] = definition
    return definitions


def catalog_version() -> str:
    return str(load_catalog_payload().get("catalog_version", "unknown"))


def incident_definition(incident_type: str) -> IncidentDefinition | None:
    return load_incident_catalog().get(str(incident_type or "Unknown"))


def all_incident_definitions() -> List[IncidentDefinition]:
    return list(load_incident_catalog().values())


def allowed_catalog_tools() -> List[str]:
    tools: List[str] = []
    for definition in all_incident_definitions():
        for tool_name in definition.discriminating_tools:
            if tool_name not in tools:
                tools.append(tool_name)
    for tool_name in PRODUCTION_READ_TOOLS:
        if tool_name not in tools:
            tools.append(tool_name)
    return tools


def allowed_catalog_actions() -> List[str]:
    actions: List[str] = []
    for definition in all_incident_definitions():
        for template in definition.safe_actions:
            action_type = str(template.get("action_type", "")).strip()
            if action_type and action_type not in actions:
                actions.append(action_type)
    return actions


def catalog_keywords(incident_type: str) -> List[str]:
    definition = incident_definition(incident_type)
    if definition is None:
        return []
    return list(definition.keywords)
