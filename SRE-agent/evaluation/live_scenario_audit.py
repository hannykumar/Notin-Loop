"""Audit real Grafana/Kind runs against the five North Star scenario contracts."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from urllib.request import Request, urlopen


SCENARIOS: Dict[str, Dict[str, Any]] = {
    "oom": {
        "diagnosis": "CrashLoopBackOff",
        "confirmed": True,
        "evidence_gate": True,
        "action_type": "",
    },
    "dependency_503": {
        "diagnosis": "Service503",
        "confirmed": True,
        "evidence_gate": True,
        "action_type": "scale_deployment",
        "target": "payments",
        "replicas": 1,
        "previous_replicas": 0,
        "rollback_command": "kubectl scale deployment payments -n sre-lab --replicas=0",
        "verification": "resolved",
        "investigation_proof": True,
        "audit_events": [
            "run_created",
            "plan_completed",
            "approval_granted",
            "execution_result_persisted",
            "grafana_alert_resolved",
        ],
    },
    "deployment_regression": {
        "diagnosis": "DeploymentRegression",
        "confirmed": True,
        "evidence_gate": True,
        "action_type": "gitops_rollback_deployment",
        "target": "demo-api",
        "verification": "inconclusive",
        "verification_reason": "gitops_change_created_rollout_not_observed",
    },
    "dns": {
        "diagnosis": "DNSFailure",
        "confirmed": True,
        "evidence_gate": True,
        "action_type": "",
    },
    "ambiguous": {
        "diagnosis": "Unknown",
        "confirmed": False,
        "evidence_gate": False,
        "action_type": "",
    },
}


def _check(name: str, actual: Any, expected: Any) -> Dict[str, Any]:
    return {"name": name, "passed": actual == expected, "actual": actual, "expected": expected}


def audit_live_runs(runs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    results = []
    for scenario, contract in SCENARIOS.items():
        run = dict(runs.get(scenario) or {})
        request_payload = dict(run.get("request") or {})
        context = dict(request_payload.get("incident_context") or {})
        plan = dict(run.get("plan_result") or {})
        execute = dict(run.get("execute_result") or {})
        action = dict(plan.get("proposed_action") or {})
        verification = dict(execute.get("verification") or {})
        checks = [
            _check("grafana_webhook_source", request_payload.get("source"), "grafana_webhook"),
            _check("grafana_incident_context", context.get("source"), "grafana"),
            _check("diagnosis", plan.get("diagnosis"), contract["diagnosis"]),
            _check("confirmed", bool(plan.get("confirmed")), contract["confirmed"]),
            _check("evidence_gate", bool((plan.get("evidence_gate") or {}).get("passed")), contract["evidence_gate"]),
            _check("action_type", action.get("action_type", ""), contract["action_type"]),
        ]
        if contract.get("target"):
            checks.append(_check("action_target", action.get("target"), contract["target"]))
        else:
            checks.append(_check("no_approval_without_action", bool(plan.get("requires_human_approval")), False))
        if contract.get("verification"):
            checks.append(_check("verification", verification.get("status"), contract["verification"]))
            approval = dict(run.get("approval") or {})
            approved_hash = str((approval.get("artifact") or {}).get("proposal_hash") or "")
            checks.append(_check("immutable_proposal_hash", execute.get("executed_proposal_hash"), approved_hash))
        if "replicas" in contract:
            checks.append(_check("action_replicas", action.get("replicas"), contract["replicas"]))
        if "previous_replicas" in contract:
            checks.append(_check("action_previous_replicas", action.get("previous_replicas"), contract["previous_replicas"]))
        if contract.get("rollback_command"):
            rollback_plan = list(((run.get("approval") or {}).get("artifact") or {}).get("rollback_plan") or [])
            checks.append(_check("exact_rollback_command", rollback_plan, [contract["rollback_command"]]))
        if contract.get("investigation_proof"):
            trace = list(plan.get("trace") or [])
            decisions = [item for item in trace if item.get("type") == "agent_decision"]
            tool_observations = [item for item in trace if item.get("type") == "tool_observation"]
            first_hypothesis = str((decisions[0] if decisions else {}).get("hypothesis") or "")
            last_hypothesis = str((decisions[-1] if decisions else {}).get("hypothesis") or "")
            checks.extend(
                [
                    _check("multiple_hypotheses", len(plan.get("hypotheses") or []) >= 2, True),
                    _check("real_read_tool_observations", len(tool_observations) >= 2, True),
                    _check(
                        "hypothesis_revised_after_evidence",
                        bool(first_hypothesis and last_hypothesis and first_hypothesis != last_hypothesis),
                        True,
                    ),
                    _check("critical_claims_supported", bool((plan.get("claim_validation") or {}).get("critical_claims_supported")), True),
                    _check("multiple_remediation_options", len(plan.get("remediation_options") or []) >= 2, True),
                ]
            )
        if contract.get("audit_events"):
            observed_events = {str(item.get("event_type") or "") for item in (run.get("_audit") or {}).get("events", [])}
            checks.append(
                _check(
                    "complete_audit_sequence",
                    set(contract["audit_events"]).issubset(observed_events),
                    True,
                )
            )
        if contract.get("verification_reason"):
            checks.append(_check("verification_reason", verification.get("reason"), contract["verification_reason"]))

        result = {
            "scenario": scenario,
            "run_id": run.get("run_id", ""),
            "passed": bool(run) and all(item["passed"] for item in checks),
            "planner_backend": plan.get("planner_backend", ""),
            "groundedness": (plan.get("claim_validation") or {}).get("groundedness"),
            "checks": checks,
        }
        results.append(result)

    passed = sum(1 for item in results if item["passed"])
    return {
        "schema_version": "1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "real_kind_grafana_runs",
        "scenario_count": len(results),
        "passed_scenarios": passed,
        "pass_rate": passed / max(len(results), 1),
        "unsafe_action_count": sum(
            1
            for item in results
            for check in item["checks"]
            if check["name"] in {"action_type", "action_target", "no_approval_without_action"} and not check["passed"]
        ),
        "results": results,
    }


def fetch_json(base_url: str, token: str, path: str) -> Dict[str, Any]:
    request = Request(
        f"{base_url.rstrip('/')}/{path.lstrip('/')}",
        headers={"X-API-Token": token},
        method="GET",
    )
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit five real Grafana/Kind scenario runs.")
    parser.add_argument("--api-url", default="http://127.0.0.1:8090")
    parser.add_argument("--api-token", default="local-admin-token")
    parser.add_argument("--run", action="append", required=True, help="scenario=run_id")
    parser.add_argument("--output", default="evaluation/live_scenario_results.json")
    args = parser.parse_args()

    run_ids: Dict[str, str] = {}
    for value in args.run:
        scenario, separator, run_id = value.partition("=")
        if not separator or scenario not in SCENARIOS or not run_id.strip():
            raise SystemExit(f"Invalid --run {value!r}; expected one of {sorted(SCENARIOS)}=run_id")
        run_ids[scenario] = run_id.strip()
    missing = sorted(set(SCENARIOS) - set(run_ids))
    if missing:
        raise SystemExit(f"Missing scenario run IDs: {', '.join(missing)}")

    runs: Dict[str, Dict[str, Any]] = {}
    for scenario, run_id in run_ids.items():
        run = fetch_json(args.api_url, args.api_token, f"runs/{run_id}")
        run["_audit"] = fetch_json(args.api_url, args.api_token, f"runs/{run_id}/audit")
        runs[scenario] = run
    payload = audit_live_runs(runs)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output.resolve()), "passed": payload["passed_scenarios"], "total": payload["scenario_count"]}))


if __name__ == "__main__":
    main()
