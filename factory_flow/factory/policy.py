"""Thresholds, scope rules, limits and SLOs (PRD K2). Frozen after K2; values are demo values, not production SLOs."""
from __future__ import annotations

import re

# --- Server ------------------------------------------------------------------
STABLE_PORT = 8101
CANARY_PORT = 8102
APP_DIR = "app/"

# --- Watch and alert -----------------------------------------------------------
SCRAPE_INTERVAL_S = 2
ALERT_WINDOW_S = 10                  # sliding window for 5xx rate and p95
ALERT_ERROR_RATE_PERCENT = 5.0       # per-path 5xx rate that counts as a breached window
ALERT_CONSECUTIVE_WINDOWS = 3

# --- Timing (normal traffic hits /reports/daily about every 20 s) --------------
CANARY_WINDOW_S = 12                 # shorter than the /reports/daily interval: canary misses it (demo 3)
WATCH_WINDOW_S = 45                  # longer than the interval: the watch window catches it
ROLLBACK_LOOKBACK_S = 120            # alert this soon after an agent release => roll that release back first

# --- Evidence gate (PRD 7.5) ---------------------------------------------------
MIN_SIGNATURE_SHARE = 0.7            # passed to evaluate_evidence_gate as minimum_confidence
REPLAY_ATTEMPTS = 3
REPLAY_REQUIRED_FAILURES = 3         # 3/3 with the same signature, else "unsure"
MIN_SOURCE_TYPES = 2                 # distinct GraphNode.source_type; the gate itself counts (type, name) pairs

# --- Reproduce and fix -----------------------------------------------------------
REPRO_RUNS = 3                       # repro test must fail 3/3 for the right reason
REPRO_ATTEMPTS = 2                   # LLM attempts before "unsure"
FIXER_TIMEOUT_S = 120

# --- Scope and diff limits ---------------------------------------------------------
MAX_FILES = 2
MAX_LINES = 20                       # added + removed lines
DENY_KEYWORDS = re.compile(r"race|concurren|intermittent|migration|auth|payment", re.IGNORECASE)
PROTECTED_PATHS = re.compile(
    r"(^|/)(tests/|requirements|pyproject|setup\.|Dockerfile|\.github/|patches/|traffic/|fallback_tests/)"
)
DEPENDENCY_FILES = re.compile(
    r"(^|/)(requirements[^/]*\.txt|pyproject\.toml|setup\.(py|cfg)|Pipfile(\.lock)?|poetry\.lock|uv\.lock)$"
)
BANNED_IMPORTS = re.compile(r"\b(import\s+(subprocess|socket|requests)\b|from\s+(subprocess|socket|requests)\s+import|os\.system|eval\(|exec\()")
# DENY_KEYWORDS must be applied to the signature, request path and only the traceback frames under APP_DIR:
# full tracebacks contain framework frames such as starlette/concurrency.py, which would match "concurren".
SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),                                         # AWS access key
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(ghp|gho|github_pat)_[A-Za-z0-9_]{20,}"),                 # GitHub tokens
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),                                  # OpenAI-style keys
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
]

# --- Canary and watch SLOs -----------------------------------------------------------
CANARY_TRAFFIC_SHARE = 0.2
SLO_MAX_ERROR_RATE_PERCENT = 1.0     # absolute cap per instance
SLO_MAX_P95_MS = 500.0               # absolute cap per instance
# Also required: classify_verification_outcome(stable, canary, {}) != "regressed".
# Its tolerance is max(10% of before, 0.1), so from a 0% baseline even 0.2% errors is "regressed";
# the absolute caps above are reported alongside it so one stray 500 is visible as the reason.
SLO_FORBIDDEN_OUTCOMES = {"regressed", "verification_timeout"}

# --- Safe replay (idempotent GETs only) ----------------------------------------------
SAFE_REPLAY_METHODS = {"GET"}
SAFE_REPLAY_PATHS = [
    re.compile(r"^/cart/total$"),
    re.compile(r"^/orders/[^/]+$"),
    re.compile(r"^/stock/[^/]+/available$"),
    re.compile(r"^/reports/daily$"),
]

# --- Bug classes ----------------------------------------------------------------------
# Same rule format as SRE-agent/config/catalog/incident_catalog.json "confirmation_rules":
# {rule_id, all|any, confidence, citations, reason}; conditions {path, equals|contains|contains_any|gte|lte|present}.
# Evaluated against dataclasses.asdict(BugReport). No matching rule => "unsure".
BUG_CLASSES = [
    {
        "rule_id": "missing_key",
        "all": [
            {"path": "exception_type", "equals": "KeyError"},
            {"path": "files[0]", "contains": APP_DIR},
            {"path": "replay_failures", "gte": REPLAY_REQUIRED_FAILURES},
        ],
        "confidence": 0.9,
        "citations": ["logs", "replay"],
        "reason": "Deterministic KeyError in application code: a missing-key guard is a small, local fix.",
    },
    {
        "rule_id": "none_access",
        "all": [
            {"path": "exception_type", "equals": "AttributeError"},
            {"path": "traceback", "contains": "NoneType"},
            {"path": "files[0]", "contains": APP_DIR},
            {"path": "replay_failures", "gte": REPLAY_REQUIRED_FAILURES},
        ],
        "confidence": 0.9,
        "citations": ["logs", "replay"],
        "reason": "Deterministic attribute access on None in application code: a None check is a small, local fix.",
    },
    {
        "rule_id": "type_mismatch",
        "all": [
            {"path": "exception_type", "equals": "TypeError"},
            {"path": "files[0]", "contains": APP_DIR},
            {"path": "replay_failures", "gte": REPLAY_REQUIRED_FAILURES},
        ],
        "confidence": 0.85,
        "citations": ["logs", "replay"],
        "reason": "Deterministic TypeError in application code: a type guard or conversion is a small, local fix.",
    },
]
