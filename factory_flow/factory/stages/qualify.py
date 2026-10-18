"""Stage 4: scope rules (PRD A8). Deny keywords, file scope, bug class, prod suite green twice."""
from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any, Dict

from factory import policy
from factory.contracts import RunState, Verdict


# Adapted from SRE-agent/agent/evidence_graph.py @ b4f7e0c (ported: that module loads the incident catalog on import).
def _joined_value(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    if isinstance(value, dict):
        return " ".join(f"{key}={item}" for key, item in value.items())
    return str(value)


def get_path(payload: Dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if part.endswith("]") and "[" in part:
            name, _, raw_index = part[:-1].partition("[")
            if name:
                if not isinstance(current, dict):
                    return None
                current = current.get(name)
            if not isinstance(current, list):
                return None
            try:
                current = current[int(raw_index)]
            except Exception:
                return None
            continue
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def evaluate_condition(payload: Dict[str, Any], condition: Dict[str, Any]) -> bool:
    value = get_path(payload, str(condition.get("path", "")))
    if "equals" in condition:
        return value == condition["equals"]
    if "contains" in condition:
        return str(condition["contains"]).lower() in _joined_value(value).lower()
    if "contains_any" in condition:
        joined = _joined_value(value).lower()
        return any(str(token).lower() in joined for token in condition.get("contains_any", []))
    if "gte" in condition:
        try:
            return float(value) >= float(condition["gte"])
        except Exception:
            return False
    if "lte" in condition:
        try:
            return float(value) <= float(condition["lte"])
        except Exception:
            return False
    if "present" in condition:
        return bool(value) is bool(condition["present"])
    return False


def evaluate_rule(payload: Dict[str, Any], rule: Dict[str, Any]) -> bool:
    if rule.get("all"):
        return all(evaluate_condition(payload, condition) for condition in rule.get("all", []))
    if rule.get("any"):
        return any(evaluate_condition(payload, condition) for condition in rule.get("any", []))
    return False
# --- end of port ---

FRAME = re.compile(r'^\s*File "([^"]+)", line \d+, in (\S+)')


def app_traceback_text(traceback: str) -> str:
    """Frame lines (plus their source line) whose file is under APP_DIR, and the final exception line."""
    lines = traceback.splitlines()
    kept = []
    for i, line in enumerate(lines):
        m = FRAME.match(line)
        if m and (m.group(1).startswith(policy.APP_DIR) or f"/{policy.APP_DIR}" in m.group(1)):
            kept.append(line)
            if i + 1 < len(lines) and not FRAME.match(lines[i + 1]):
                kept.append(lines[i + 1])
    tail = [l for l in lines if l.strip()]
    if tail and not FRAME.match(tail[-1]):
        kept.append(tail[-1])
    return "\n".join(kept)


def _suite(workspace, prod_ref: str) -> tuple[list[dict], list[str]]:
    results = []
    for _ in range(2):
        results.append(workspace.run_pytest(workspace.fresh_worktree(prod_ref)))
    failing = [{f["nodeid"] for f in r["failures"]} for r in results]
    return results, sorted(failing[0] ^ failing[1])


def run(state: RunState, *, workspace=None, prod_ref: str = "prod") -> Verdict:
    bug = state.bug
    if bug is None:
        return Verdict("qualify", "unsure", ["no bug report"], {"bug_report_present": False}, {})

    deny_text = "\n".join([bug.signature, str(bug.sample_request.get("path", "")), app_traceback_text(bug.traceback)])
    deny = policy.DENY_KEYWORDS.search(deny_text)
    bad_files = [f for f in bug.files if policy.PROTECTED_PATHS.search(f) or policy.DEPENDENCY_FILES.search(f)
                 or not f.startswith(policy.APP_DIR)]
    rule = next((r for r in policy.BUG_CLASSES if evaluate_rule(asdict(bug), r)), None)
    checks = {
        "bug_report_present": True,
        "no_deny_keywords": deny is None,
        "files_within_limit": 0 < len(bug.files) <= policy.MAX_FILES,
        "files_in_scope": not bad_files,
        "bug_class_known": rule is not None,
    }
    evidence: dict = {"rule_id": rule and rule["rule_id"], "rule_reason": rule and rule["reason"],
                      "deny_match": deny and deny.group(0), "out_of_scope_files": bad_files, "flaky_tests": []}
    reasons = []
    if deny:
        reasons.append(f"deny keyword {deny.group(0)!r}")
    if not (checks["files_within_limit"] and checks["files_in_scope"]):
        reasons.append(f"scope violation: {len(bug.files)} files, out of scope {bad_files}")
    if not rule:
        reasons.append("bug class not in policy")
    if reasons:  # don't spend a test run on a report that can't qualify
        checks["prod_suite_green"] = False
        status = "fail" if not (checks["no_deny_keywords"] and checks["files_within_limit"]
                                and checks["files_in_scope"]) else "unsure"
        return Verdict("qualify", status, reasons, checks, {**evidence, "checks": checks})

    if workspace is None:
        try:  # STUB(track B): factory/workspace.py (B2) may not be ready; used as a module exposing the Workspace API
            from factory import workspace as ws_module
        except ImportError:
            ws_module = None
        if not (hasattr(ws_module, "fresh_worktree") and hasattr(ws_module, "run_pytest")):
            checks["prod_suite_green"] = False
            return Verdict("qualify", "unsure", ["workspace unavailable"], checks, {**evidence, "checks": checks})
        workspace = ws_module
    try:
        results, flaky = _suite(workspace, prod_ref)
    finally:
        if hasattr(workspace, "cleanup"):
            workspace.cleanup()
    green = not flaky and all(r["failed"] == 0 and r["errors"] == 0 and r["passed"] > 0 for r in results)
    checks["prod_suite_green"] = green
    evidence.update(flaky_tests=flaky, suite_runs=[{k: r[k] for k in ("passed", "failed", "errors", "junit_path")}
                                                   for r in results], checks=checks)
    if not green:
        return Verdict("qualify", "fail", [f"prod suite not green{' (flaky: ' + ', '.join(flaky) + ')' if flaky else ''}"],
                       checks, evidence)
    return Verdict("qualify", "pass", [f"bug class {rule['rule_id']}"], checks, evidence)
