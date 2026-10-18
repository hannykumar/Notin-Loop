from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Tuple
import re


MAX_RANGE_SECONDS = 2 * 60 * 60
MAX_QUERY_LENGTH = 800
MAX_RESULT_LIMIT = 500


def validate_kubernetes_name(value: str, *, namespace: bool = False) -> str:
    limit = 63 if namespace else 253
    label = r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?"
    pattern = label if namespace else rf"{label}(?:\.{label})*"
    if not isinstance(value, str) or len(value) > limit or not re.fullmatch(pattern, value):
        raise ValueError("Invalid Kubernetes namespace or resource name")
    return value


def validate_query_text(query: str) -> str:
    value = str(query or "").strip()
    if not value or len(value) > MAX_QUERY_LENGTH:
        raise ValueError("Query must be non-empty and at most 800 characters")
    if ";" in value or "\x00" in value:
        raise ValueError("Query contains a forbidden separator")
    return value


def validate_limit(limit: int) -> int:
    value = int(limit)
    if value < 1 or value > MAX_RESULT_LIMIT:
        raise ValueError(f"Result limit must be between 1 and {MAX_RESULT_LIMIT}")
    return value


def validate_time_range(start: str, end: str) -> Tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    end_at = datetime.fromisoformat(str(end).replace("Z", "+00:00")) if end else now
    start_at = datetime.fromisoformat(str(start).replace("Z", "+00:00")) if start else end_at - timedelta(minutes=15)
    start_at = start_at.astimezone(timezone.utc)
    end_at = end_at.astimezone(timezone.utc)
    if start_at >= end_at:
        raise ValueError("Query start must be before query end")
    if (end_at - start_at).total_seconds() > MAX_RANGE_SECONDS:
        raise ValueError("Query time range exceeds the two-hour safety limit")
    return start_at, end_at
