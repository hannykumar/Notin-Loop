# Rebuild status — September 17, 2026

The replacement workbench and backend fixes are implemented. Final authenticated visual verification passed on September 17; the focused local rebuild is complete. This is a practical portfolio product, not a production certification.

## What changed

- Replaced Streamlit with a native browser workbench: incident → investigation → exact approval → verification, plus model evaluation and runtime access.
- Archived retired Streamlit source, its display tests and unused Qdrant configuration. Preserved the pre-existing repository migration.
- Fixed atomic approval claims, duplicate execution protection, preview writes, rollback snapshots, path/query boundaries, namespace propagation and unsafe replica assumptions.
- Made model failures visible across all planning steps; bounded output explanations to prevent small-model JSON truncation.
- Added readable evidence, competing hypotheses, citations, recovery measurements and per-case evaluation results.
- Fixed lab restarts: explicit Kind context, refreshed demo pods, persistent ignored kubeconfig, readiness waits, bounded admin HTTP calls and no unnecessary Postgres host port.

## Verified evidence

| Check | Result | Scope |
| --- | --- | --- |
| Packaged Python 3.12 suite | 115 passed, 1 skipped | Built application image; host-only jq check skipped |
| Host suite | 113 passed, 3 skipped | Rechecked September 17 before repository publication; optional dependencies absent |
| Latest focused checks | 7 passed; lab-scope check passed after readiness change | Model schema, GitOps image handling and lab context |
| Frontend | Node assertions passed; investigation and fixture approval exercised in Chrome | Authenticated recovery screen visually verified in Chrome on September 17 |
| Ollama llama3.2:1b benchmark | 100% model coverage, zero fallback rows in all three model profiles | 20 fixture-derived cases per profile, preview only |
| Workflow diagnosis accuracy | Hybrid 100%, deterministic 100%, model-only/RAG-only 20% | Does not demonstrate added diagnostic value from the LLM |
| Fresh live dependency repair | Resolved with matching proposal hashes and three recovery samples | Deterministic planner, real Kind writes, September 16 |

Live run `run_f93170d672` restored only `payments` in `sre-lab` from zero to one replica. Error rate fell from **45.79% to 7.79%**, passing the configured **20% demo threshold**. Log absence passed; latency was unavailable and optional. The lab reset completed afterward. This is measured demo recovery, not normal service or a production SLO.

Local evidence: `eval_history/workbench_latest.json`, `eval_history/live-repair-20260916.json` and `eval_history/live-repair-20260916-audit.json`. Historical five-scenario evidence remains in `evaluation/live_scenario_results.json`; it is not a fresh verification of all five scenarios.

## Completion

With explicit user authorization, signed into `http://127.0.0.1:8090` and visually inspected the Verification tab for `run_f93170d672` on September 17. The rendered screen shows resolved recovery, completed execution, three samples, matched approval integrity, the saved 45.79% → 7.79% error-rate change, optional unavailable latency, and passed log absence. The layout is readable. This was a read-only review of the September 16 result, not a new repair or rerun of the test suite.

The completion audit rechecked the current entry points, archived implementations, four saved benchmark profiles (20 rows each, complete, no fallback rows), and the saved live result (live mode, resolved, matching hashes, three samples). Active startup, dependencies and frontend contain no Streamlit references. Repository simplification, verified bug fixes, small-model evaluation and live execution evidence are present. The final rendered recovery screen is now verified; all recorded completion gates for the focused rebuild are closed.

The earlier interrupted run `run_1c23366209` remains `executing` with an `in_progress` execution record and no verification result. Infrastructure was inspected before the later fresh drill; the uncertain action was not replayed or relabelled as successful.

## Limits and next development priorities

- Redis has no recovery for jobs lost after popping; interrupted execution requires operator investigation.
- Groundedness validates two structured claims, not all prose. Confidence is heuristic.
- GitOps creates local artifacts; remote review, merge and controller rollout are not implemented.
- The full Kind monitoring stack is heavy on an 8 GB Mac. Use the fixture profile for routine development and the small model for local inference.
- Production use needs service-specific thresholds, stronger operational recovery, broader evaluations and independent review.

The repository update includes the application rebuild and supporting documents. No production deployment was performed. Detailed rebuild history is preserved locally in `archive/workbench_rebuild_2026-09-07/REBUILD_HISTORY.md`. Start with [README](../README.md), [project map](../PROJECT_STRUCTURE.md) and [architecture](ARCHITECTURE_SIMPLE.md).
