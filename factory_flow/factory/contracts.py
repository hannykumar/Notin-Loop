"""Frozen data contracts shared by both tracks (PRD 7.4). Change only after telling the other track.

Additions to PRD 7.4 (approved in K2): Verdict.route, Release.files_touched, Release.baseline, RunState.
Shapes follow the real SRE-agent code; see the notes on MetricsSnapshot and GateCandidate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional, Protocol, TypedDict

Status = Literal["pass", "fail", "unsure"]
ReleaseStatus = Literal["watching", "kept", "rolled_back"]


@dataclass(frozen=True)
class Verdict:
    stage: str
    status: Status
    reasons: list[str]
    checks: dict[str, bool]       # all True => "pass"
    evidence: dict[str, Any]      # artifact paths + SHA-256 hashes
    route: Optional[str] = None   # e.g. "rollback" from triage; None = default (continue or escalate)


@dataclass(frozen=True)
class BugReport:                  # written by the investigator
    id: str
    alert_id: str
    signature: str                # "KeyError@app/cart.py:cart_total"
    exception_type: str
    traceback: str
    sample_request: dict          # method, path, query, body
    files: list[str]
    error_share: float
    replay_failures: int          # out of 3
    evidence_ids: list[str]


@dataclass(frozen=True)
class Release:
    tag: str                      # "release-2"
    commit: str
    previous_tag: str
    diff_sha256: str
    run_id: str
    promoted_at: str              # ISO 8601 UTC
    watch_until: str              # ISO 8601 UTC
    status: ReleaseStatus
    files_touched: list[str] = field(default_factory=list)  # for "no new signatures in touched files"
    baseline: dict = field(default_factory=dict)            # MetricsSnapshot before promotion; rollback verifies against it


@dataclass
class RunState:
    """Mutable per-run state. Stages fill in their own fields and return a Verdict."""
    run_id: str
    alert: dict                               # output of normalize_generic_alert
    artifacts_dir: Path                       # runs/artifacts/<run_id>/
    bug: Optional[BugReport] = None           # investigate
    failing_test: Optional[Path] = None       # reproduce (frozen copy)
    test_sha256: str = ""                     # reproduce
    diff: str = ""                            # fix
    diff_sha256: str = ""                     # oracle; recomputed by canary and promote
    release: Optional[Release] = None         # promote, or the release triage routes to rollback
    verdicts: list[Verdict] = field(default_factory=list)


class Fixer(Protocol):
    name: str
    def fix(self, workspace: Path, bug: BugReport, failing_test: Path, timeout_s: int) -> str:
        """Return a unified diff. Nothing else from the fixer is read."""


class Stage(Protocol):
    def __call__(self, state: RunState) -> Verdict:
        """Every stage module exposes: def run(state: RunState) -> Verdict"""


# --- Shared interface shapes -------------------------------------------------

class Metrics(TypedDict, total=False):
    error_rate_percent: float     # 5xx share of requests in the window, 0-100
    p95_latency_ms: float


class MetricsSnapshot(TypedDict):
    """Returned by watcher.baseline/current.

    Real shape required by sre_base.classify_verification_outcome(before, after, rule_verification):
    it reads before["metrics"] / after["metrics"], not top-level keys. Pass rule_verification={}.
    """
    metrics: Metrics
    instance: Literal["stable", "canary"]
    path: str
    window_s: float
    samples: int                  # requests seen in the window; 0 means no evidence => "unsure"


class PytestFailure(TypedDict):
    nodeid: str
    type: str                     # exception type, e.g. "AssertionError", "KeyError", "ImportError"
    message: str


class PytestResult(TypedDict):
    passed: int
    failed: int
    errors: int                   # collection/import/setup errors, never a valid "red"
    skipped: int
    failures: list[PytestFailure]
    junit_path: str
    exit_code: int


class GateCandidate(TypedDict):
    """Input to sre_base.evaluate_evidence_gate (real SRE-agent shape)."""
    score: float                          # compared with minimum_confidence; margin vs 2nd candidate >= 0.08
    citations: list[str]                  # evidence_ids present in the graph
    contradicting_signals: list[str]      # only "critical:", "contradicted:", "rules out:" prefixes block


class GraphNode(TypedDict):
    """Evidence graph node. The gate counts distinct (source_type, source_name) pairs as independent."""
    evidence_id: str
    source_type: str              # "alert" | "logs" | "metrics" | "replay" | "releases"
    source_name: str


class AuditLog(Protocol):
    """factory/audit.py (A2). Entry format: PRD 7.7."""
    def append(self, event: dict) -> dict:
        """Add seq, ts, prev_hash, entry_hash, hmac; write to runs/audit.jsonl; return the full entry."""
    def verify(self) -> tuple[bool, Optional[int]]:
        """(ok, first_bad_seq)."""


class Watcher(Protocol):
    """factory/watcher.py (A4)."""
    def baseline(self, path: str, instance: str = "stable") -> MetricsSnapshot:
        """Metrics before the incident / release."""
    def current(self, path: str, instance: str = "stable") -> MetricsSnapshot:
        """Metrics over the latest sliding window."""


class Workspace(Protocol):
    """factory/workspace.py (B2)."""
    def fresh_worktree(self, ref: str) -> Path:
        """New git worktree of target_service at ref, in a temp dir."""
    def run_pytest(self, path: Path, selector: str) -> PytestResult:
        """Run pytest with --junitxml inside path (own cwd and PYTHONPATH)."""


# runs/releases.json: {"releases": [dataclasses.asdict(Release), ...]} in promotion order.
RELEASES_FILE_KEY = "releases"
