"""Controlled four-profile benchmark for the SRE investigation workflow.

Model profiles require at least 90% model coverage. Accuracy describes the
complete workflow, including deterministic controls and any reported fallback;
it is not a raw model classification score.
"""

from __future__ import annotations

import argparse
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator

from agent.model_runtime import active_model_status, reset_model_runtime_state
from evaluation.runner import compute_aggregates, evaluate_incident
from evaluation.replays import generate_replayed_incidents
from runtime.settings import reset_settings_cache


PROFILES: Dict[str, Dict[str, Any]] = {
    "deterministic_baseline": {
        "provider": "deterministic",
        "retrieval": True,
        "tools": True,
        "claim_validation": True,
        "description": "Deterministic planner with the same retrieval, tools, and safety boundary.",
    },
    "llm_no_retrieval": {
        "provider": "model",
        "retrieval": False,
        "tools": False,
        "claim_validation": False,
        "description": "Model sees only alert context; no runbook retrieval or telemetry tools.",
    },
    "llm_rag": {
        "provider": "model",
        "retrieval": True,
        "tools": False,
        "claim_validation": False,
        "description": "Model receives retrieved runbooks but cannot collect live evidence.",
    },
    "hybrid_full": {
        "provider": "model",
        "retrieval": True,
        "tools": True,
        "claim_validation": True,
        "description": "Model, runbook retrieval, adaptive read tools, and deterministic evidence controls.",
    },
}


@contextmanager
def _planner_runtime(provider: str, model: str = "") -> Iterator[None]:
    key = "SRE_AGENT_PLANNER_PROVIDER"
    model_key = "SRE_AGENT_PLANNER_MODEL"
    previous = os.environ.get(key)
    previous_model = os.environ.get(model_key)
    os.environ[key] = provider
    if model:
        os.environ[model_key] = model
    reset_settings_cache()
    reset_model_runtime_state()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous
        if previous_model is None:
            os.environ.pop(model_key, None)
        else:
            os.environ[model_key] = previous_model
        reset_settings_cache()
        reset_model_runtime_state()


def _unavailable(profile: str, reason: str, *, rows: list[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    return {
        "profile": profile,
        "status": "unavailable",
        "reason": reason,
        "capabilities": PROFILES[profile],
        "aggregates": {},
        "rows": rows or [],
    }


def _run_profile(profile: str, *, execution_mode: str, isolate_model_runs: bool) -> Dict[str, Any]:
    rows = []
    consecutive_fallbacks = 0
    for incident_payload in generate_replayed_incidents():
        if isolate_model_runs:
            reset_model_runtime_state()
        row = evaluate_incident(
            str(incident_payload["incident_id"]),
            tool_mode="mcp",
            execution_mode=execution_mode,
            incident_payload=incident_payload,
            evaluation_profile=profile,
            run_execution_pass=False,
        )
        rows.append(row)
        if row.get("planner_error") or row.get("planner_fallback_count") or "fallback" in str(row.get("planner_backend", "")):
            consecutive_fallbacks += 1
        else:
            consecutive_fallbacks = 0
        if len(rows) <= 3 and consecutive_fallbacks >= 3:
            break
    return {"aggregates": compute_aggregates(rows), "rows": rows}


def run_benchmark_matrix(
    *,
    model_provider: str,
    model: str = "",
    execution_mode: str = "preview",
    save_path: str | Path | None = "evaluation/benchmark_matrix_results.json",
) -> Dict[str, Any]:
    configured_model_provider = str(model_provider or "").strip().lower()
    model_probe: Dict[str, Any] = {}
    if configured_model_provider and configured_model_provider != "deterministic":
        with _planner_runtime(configured_model_provider, model):
            model_probe = active_model_status(force=True)

    results: Dict[str, Any] = {}
    for profile, capabilities in PROFILES.items():
        provider = "deterministic" if capabilities["provider"] == "deterministic" else configured_model_provider
        if capabilities["provider"] == "model" and (
            not provider or provider == "deterministic" or not model_probe.get("ready")
        ):
            results[profile] = _unavailable(profile, str(model_probe.get("probe_error") or "No reachable model provider configured."))
            continue

        with _planner_runtime(provider, model if capabilities["provider"] == "model" else ""):
            evaluation = _run_profile(
                profile,
                execution_mode=execution_mode,
                isolate_model_runs=capabilities["provider"] == "model",
            )
        fallback_rows = [
            row
            for row in evaluation["rows"]
            if row.get("planner_error") or row.get("planner_fallback_count") or "fallback" in str(row.get("planner_backend", ""))
        ]
        model_coverage = 1.0 - (len(fallback_rows) / max(len(evaluation["rows"]), 1))
        if capabilities["provider"] == "model" and (len(evaluation["rows"]) < 20 or model_coverage < 0.9):
            results[profile] = _unavailable(
                profile,
                f"Model coverage was {model_coverage:.0%} across {len(evaluation['rows'])}/20 replays; at least 90% is required.",
                rows=evaluation["rows"],
            )
            continue
        results[profile] = {
            "profile": profile,
            "status": "complete",
            "capabilities": capabilities,
            "aggregates": evaluation["aggregates"],
            "model_coverage": round(model_coverage, 3),
            "fallback_rows": len(fallback_rows),
            "rows": evaluation["rows"],
        }

    hybrid = results.get("hybrid_full", {})
    comparison = {
        "claim": "not_proven",
        "reason": "A complete deterministic baseline and model-backed hybrid run are required.",
    }
    completed_baselines = [
        results[name]
        for name in ("deterministic_baseline", "llm_no_retrieval", "llm_rag")
        if results.get(name, {}).get("status") == "complete"
    ]
    if hybrid.get("status") == "complete" and completed_baselines:
        hybrid_metrics = hybrid["aggregates"]
        comparisons = []
        for baseline in completed_baselines:
            baseline_metrics = baseline["aggregates"]
            comparisons.append(
                {
                    "baseline": baseline["profile"],
                    "type_accuracy_delta": round(
                        float(hybrid_metrics.get("type_accuracy", 0.0))
                        - float(baseline_metrics.get("type_accuracy", 0.0)),
                        4,
                    ),
                    "unsafe_execution_rate_delta": round(
                        float(hybrid_metrics.get("unsafe_execution_rate", 0.0))
                        - float(baseline_metrics.get("unsafe_execution_rate", 0.0)),
                        4,
                    ),
                }
            )
        qualifying = [
            item
            for item in comparisons
            if item["type_accuracy_delta"] > 0 and item["unsafe_execution_rate_delta"] <= 0
        ]
        comparison = {
            "claim": "provisional_preview_evidence" if qualifying else "not_proven",
            "qualifying_baselines": [item["baseline"] for item in qualifying],
            "comparisons": comparisons,
            "reason": (
                "Hybrid improved type accuracy over at least one simpler profile with no preview-policy safety regression; "
                "a real execution benchmark is still required for the production-safety claim."
            ),
        }

    payload = {
        "schema_version": "1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_provider": configured_model_provider or "not_configured",
        "model": model or str(model_probe.get("model") or "default"),
        "safety_scope": "preview_policy_only",
        "model_probe": model_probe,
        "profiles": results,
        "hybrid_vs_baseline": comparison,
    }
    if save_path:
        output = Path(save_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(output)
        payload["result_path"] = str(output.resolve())
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the controlled four-profile SRE benchmark.")
    parser.add_argument("--model-provider", default=os.getenv("SRE_AGENT_PLANNER_PROVIDER", ""))
    parser.add_argument("--model", default=os.getenv("SRE_AGENT_PLANNER_MODEL", ""))
    parser.add_argument("--execution-mode", choices=["preview", "live"], default="preview")
    parser.add_argument("--output", default="evaluation/benchmark_matrix_results.json")
    args = parser.parse_args()
    payload = run_benchmark_matrix(
        model_provider=args.model_provider,
        model=args.model,
        execution_mode=args.execution_mode,
        save_path=args.output,
    )
    print(json.dumps({"result_path": payload.get("result_path"), "hybrid_vs_baseline": payload["hybrid_vs_baseline"]}, indent=2))


if __name__ == "__main__":
    main()
