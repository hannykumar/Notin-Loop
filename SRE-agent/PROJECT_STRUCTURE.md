# Project map

Read the active request flow in this order:

1. `frontend/app.js` — browser workbench and API requests.
2. `runtime/api.py` — incidents, approvals and static frontend.
3. `runtime/job_runner.py` — investigation and approved execution jobs.
4. `agent/langgraph_agent.py` — bounded investigation graph.
5. `agent/planner.py` — structured model output and deterministic fallback.
6. `integrations/tool_gateway.py` — read-tool transport.
7. `executor/engine.py` — action execution and recovery measurements.

| Directory | Owns |
| --- | --- |
| `frontend/` | Native browser modules, HTML and CSS; served by FastAPI |
| `runtime/` | API, persistence, jobs, authentication, health and isolated evaluation jobs |
| `agent/` | Hypotheses, retrieval, evidence validation, proposals and summaries |
| `integrations/` | MCP, Kubernetes, Prometheus, Loki and bounded queries |
| `executor/` | Exact execution, GitOps artifacts, rollback and verification |
| `evaluation/` | Replay comparison, live scenario audits and historical evidence |
| `tests/` | Active regression and workflow checks |
| `lab/` | Fixtures, demo service, Kind workloads and fault/reset scripts |
| `config/` | Incident contracts and service context |
| `runbooks/` | Four focused diagnostic runbooks |
| `scripts/`, `docker/` | Startup and optional infrastructure |
| `docs/` | Architecture, rebuild audit and follow-on scope |
| `archive/` | Recoverable retired implementations; ignored by Git and tests |

`runtime.db`, `data/mock_live_state/`, `traces/`, `artifacts/` and `eval_history/` are generated local data. `NORTH_STAR.md` records the original scope and dated portfolio proof; `docs/REBUILD.md` tracks the current migration and remaining checks.
