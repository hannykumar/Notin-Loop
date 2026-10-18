# Kubernetes incident lab

This lab provides real Kubernetes workload state for five focused scenario contracts: OOM, dependency 503, deployment regression, DNS failure, and an intentionally ambiguous alert. The agent runs through Docker Compose while its live MCP backend reads the Kind cluster over an internal kubeconfig and stable Kind-network monitoring endpoints.

Prerequisites: Docker, Kind, kubectl, Helm, `curl`, `jq`, and `rg`.

```bash
./lab/scripts/kind_lab_up.sh
./lab/scripts/kind_lab_agent_up.sh
./lab/scripts/kind_lab_fault.sh dependency-503
```

`kind_lab_up.sh` creates the cluster, loads the demo image, installs Prometheus/Grafana and Loki, applies the ServiceMonitor and rules, and idempotently provisions the Grafana contact point, policy, folder, and five managed alerts with a 10-second evaluation interval. Override `SRE_LAB_WEBHOOK_URL` if the Ops API is not reachable at the default `host.docker.internal:8090` address.

`kind_lab_agent_up.sh` refreshes a Kind-internal kubeconfig at `.env.lab-kubeconfig` (ignored by Git, readable only by its owner), attaches the API, worker, executor, and write gateway to the `kind` Docker network, uses `sre-agent-lab-control-plane:30900` for Prometheus, and starts an executor-only `kubectl-mcp` gateway. Read tools are live and read-only. Logs are bounded from the alert start time so an earlier scenario cannot contaminate a later diagnosis. The write gateway allows only `sre-lab`, requires its approval token, and is reachable only from the container networks.

Wait for Grafana to create a run, then inspect it in the UI or API. The expected state before execution is `awaiting_approval`. Approve the exact immutable proposal from the Approval page, or in the disposable lab:

```bash
curl -X POST http://127.0.0.1:8090/runs/RUN_ID/approve-execute \
  -H 'X-API-Token: local-admin-token' \
  -H 'Content-Type: application/json' \
  --data-binary '{"execution_mode":"live"}'
```

The API and job runner check the proposal hash; the internal executor performs the stored action, samples post-change evidence, and records one of `resolved`, `improved`, `unchanged`, `regressed`, `inconclusive`, or `verification_timeout`. Confirm Grafana has cleared the alert, then reset:

```bash
./lab/scripts/kind_lab_reset.sh
```

Fault names are `oom`, `dependency-503`, `deployment-regression`, `dns`, and `ambiguous`. Reset removes the OOM workload, restores payments, removes the bad-release annotation, and returns the API to healthy mode.

On a Docker VM with about 4 GB memory, the complete monitoring stack may produce transient Grafana `DatasourceError` events. Those Grafana rule-health meta-alerts are acknowledged but deliberately not turned into service-remediation runs. For the smallest live demo, pause nonessential UIs and optional telemetry after proving their connectivity:

```bash
docker compose stop pgadmin redisinsight
kubectl scale statefulset/loki -n monitoring --replicas=0
kubectl scale deployment/loki-gateway -n monitoring --replicas=0
kubectl scale statefulset/alertmanager-monitoring-kube-prometheus-alertmanager -n monitoring --replicas=0
```

## Audited proof

The historical five-run audit is checked in at `evaluation/live_scenario_results.json`. The following command requires those runs to exist in your database; it does not create a fresh drill:

```bash
PYTHONPATH=. .venv/bin/python evaluation/live_scenario_audit.py \
  --run oom=run_8e78deb7df \
  --run dependency_503=run_b7883371b1 \
  --run deployment_regression=run_2b8e36d636 \
  --run dns=run_c3bed86b9e \
  --run ambiguous=run_711eb08e2f
```

The audit passes all five contracts and reports zero unsafe scenario actions. The dependency run used `ollama_guardrail`, changed its hypothesis after real read-tool observations, scaled only `payments` from zero to one, preserved an exact rollback to zero, matched the approved and executed hashes, reached `resolved` after four samples as the error rate fell from `58.70%` to `0%`, and recorded Grafana's resolved notification. The deployment action intentionally remains `inconclusive` because this lab creates a GitOps artifact but does not claim that an external delivery controller merged and rolled it out.

## Five-minute incident demonstration

After the stack is already running, the operator-facing drill fits into one short session:

1. Run `./lab/scripts/kind_lab_reset.sh`, then `./lab/scripts/kind_lab_fault.sh dependency-503`.
2. Open **Incidents**, refresh the browser, and open the webhook-created run when it appears.
3. Review the hypotheses, cited evidence, exact action, risk, rollback-to-zero command, and proposal hash.
4. Approve live execution once; do not modify the proposal.
5. Show the before/after error rate, repeated samples, matching hashes, `resolved` outcome, Grafana resolution audit event, and complete audit trail.

Do not use the earlier run `run_9144b76fed` as evidence. Its API-pod restart also reset in-memory fault state while the dependency stayed down, so the apparent recovery was not causally valid.
