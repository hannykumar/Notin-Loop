# SRE Copilot

> **Rebuild status:** The September 2026 workbench rebuild is complete. Final authenticated recovery-screen verification passed on September 17, 2026. This is a focused portfolio prototype, not a production certification.

An evidence-grounded incident investigation and human-approved remediation workbench. A master's portfolio project using Kubernetes, Grafana, Prometheus, Loki, LangGraph and a local LLM.

**Workflow:** alert or scenario → investigation → exact approval → execution → measured recovery. A separate evaluation screen compares deterministic, LLM-only, retrieval and hybrid profiles.

## Run locally

Python 3.11 or 3.12 is recommended. No frontend build or Node service is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
bash scripts/start_stack.sh
```

Open **http://localhost:8090**. The default profile uses SQLite, fixture-backed evidence and deterministic planning. Choose the dependency 503 scenario, review the evidence, then approve a preview or an explicitly labelled fixture simulation.

Local services bind to loopback. Container startup sets `SRE_BIND_HOST=0.0.0.0`. The executor listens on port 8091; configure `EXECUTOR_SHARED_TOKEN` and API authentication before exposing services beyond your machine.

## Use the local LLM

On an 8 GB Mac, start with the small model already used in this project's evaluation:

```bash
ollama serve
# In another terminal, if the model is not installed:
ollama pull llama3.2:1b

bash scripts/start_stack.sh --llm
```

Use **Runtime & access** to check model availability. Each investigation displays its actual planner backend and any fallback error. A reachable server is not proof of successful inference. The full investigation has a separate `SRE_INVESTIGATION_TIMEOUT_SECONDS` budget, defaulting to 600 seconds.

## Evaluate

Open **Model evaluation** to inspect saved evidence or start a new preview benchmark. Web-triggered benchmarks run in a separate process with their own database and fixture state; results go to ignored `eval_history/`, preserving historical portfolio evidence.

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q
PYTHONPATH=. .venv/bin/python -m evaluation.benchmark_matrix \
  --model-provider ollama --model llama3.2:1b \
  --output eval_history/comparison.json
```

Historical evidence covers 20 fixture-derived replays per completed profile and five Kind/Grafana scenario contracts. Hybrid accuracy matched the deterministic baseline and exceeded LLM-only accuracy. These small local results do not establish general production safety. [NORTH_STAR.md](NORTH_STAR.md) records dated evidence and limitations; [docs/REBUILD.md](docs/REBUILD.md) contains the current audit.

Groundedness currently validates two structured claims (alert and diagnosis), not all model-written prose. Confidence is a heuristic ranking, not a calibrated probability. Recovery is judged against catalog thresholds: the demo 503 threshold is 20% errors, which is not a production SLO. Configure service-specific recovery criteria before operational use.

Benchmark accuracy is the workflow's final diagnosis after deterministic controls, including explicitly reported fallbacks. Profiles with tools disabled also lose the evidence required by those controls; their scores do not measure raw model intelligence. The deterministic baseline is the comparison that tests whether adding the model improves this workflow.

## Real Kubernetes lab

```bash
./lab/scripts/kind_lab_up.sh
./lab/scripts/kind_lab_agent_up.sh
./lab/scripts/kind_lab_fault.sh dependency-503
./lab/scripts/kind_lab_reset.sh
```

See [lab/k8s/README.md](lab/k8s/README.md). Cases cover OOM, dependency 503, deployment regression, DNS failure and uncertain escalation. Live execution requires explicit approval. GitOps proposals remain `inconclusive` until an external rollout is observed.

For the larger Postgres/Redis container profile, use `docker compose up --build`. Optional database admin UIs require `--profile admin-tools`; the observability demo uses `--profile alert-lab`.

## Read the code

Start with [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md), then [docs/ARCHITECTURE_SIMPLE.md](docs/ARCHITECTURE_SIMPLE.md). Active product code lives in `agent/`, `runtime/`, `integrations/`, `executor/` and `frontend/`. Evaluation checks behavior; lab fixtures, configuration and runbooks define the focused scenarios.

Old implementations remain in ignored `archive/`. Runtime databases, traces, artifacts and evaluation history are generated local data.
