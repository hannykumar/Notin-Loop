import json
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4
from integrations.incident_registry import validate_incident_id


TRACES_DIR = Path("traces")


def now_ts() -> str:
    # e.g. 2025-12-26T14-03-22
    return datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


@dataclass
class TraceEvent:
    name: str
    data: Dict[str, Any]


@dataclass
class IncidentTrace:
    trace_id: str
    incident_id: str
    created_at: str
    events: List[TraceEvent] = field(default_factory=list)

    def add(self, name: str, **data: Any) -> None:
        self.events.append(TraceEvent(name=name, data=data))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "incident_id": self.incident_id,
            "created_at": self.created_at,
            "events": [{"name": e.name, "data": e.data} for e in self.events],
        }

    def save(self) -> Path:
        TRACES_DIR.mkdir(exist_ok=True)
        fp = TRACES_DIR / f"{validate_incident_id(self.incident_id)}_{validate_incident_id(self.trace_id)}.json"
        fp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return fp


def create_trace(incident_id: str) -> IncidentTrace:
    trace_id = f"{now_ts()}_{uuid4().hex[:8]}"
    return IncidentTrace(trace_id=trace_id, incident_id=incident_id, created_at=datetime.now().isoformat())
