# North Star: Evidence-Grounded AI SRE Agent

## Product Goal

Build a focused, industry-style prototype of an AI SRE agent:

> A Grafana-triggered AI agent that investigates Kubernetes incidents using real operational telemetry, produces evidence-grounded root-cause hypotheses and remediation options, executes one exact human-approved action through a deterministic safety layer, and verifies whether the service recovered.

This is a master's and portfolio project, not an attempt to build an entire commercial AIOps platform. It should prove one workflow deeply:

1. a real alert fires
2. the agent understands the actual incident context
3. the agent investigates using read-only tools
4. evidence changes its hypotheses
5. the agent explains what happened and why
6. it recommends safe remediation options
7. a human approves one exact proposal
8. deterministic code executes only that proposal
9. deterministic verification measures recovery
10. the complete investigation remains auditable

## Product Positioning

Project name:

**Evidence-Grounded AI SRE Agent for Production Incident Response**

Short description:

> A Grafana-triggered AI agent that investigates Kubernetes incidents through Prometheus metrics, Loki logs, deployment history, runbooks, and infrastructure state. It generates and revises root-cause hypotheses, produces evidence-cited remediation options, and executes only an immutable human-approved action through a deterministic safety layer.

Do not describe the project as fully autonomous, production deployed, or completely self-healing unless those claims become demonstrably true.

## Focused Incident Scope

Support four diagnosable incident classes and one uncertainty case:

1. `CrashLoopBackOff` caused by OOM or memory pressure
2. `Service503` caused by an upstream dependency failure
3. `DeploymentRegression` that also produces 503 symptoms
4. `DNSFailure` caused by name-resolution problems
5. `Unknown` or ambiguous incidents that must escalate

The two 503 scenarios are essential. They demonstrate that the agent distinguishes different root causes behind the same visible symptom instead of performing keyword classification.

Do not add more incident classes until these cases work end to end with real evidence and difficult variations.

## Target Technology Scope

- Grafana Alerting: incident trigger and dashboard context
- Prometheus: metrics
- Loki: logs
- Kubernetes API: workloads, pod state, deployments, configuration, and events
- Git or GitOps history: recent changes and rollback target
- LangGraph: bounded investigation workflow
- Ollama or an OpenAI-compatible model: hypothesis generation and reasoning
- PostgreSQL: incidents, runs, evidence, approvals, execution, verification, and audit
- Redis: optional durable background jobs and deduplication
- FastAPI: backend API
- Native browser workbench served by FastAPI (replaced Streamlit during the 2026-09-07 rebuild)

Tempo and additional commercial integrations are deferred until the core workflow is complete.

## Target Architecture

```text
Grafana Alert
      |
      v
Real Incident Context
      |
      v
AI Investigator
  - generate hypotheses
  - identify missing evidence
  - select read-only tools
  - review observations
  - revise hypotheses
      |
      v
Deterministic Evidence Gate
      |
      v
AI Incident Report and Remedy Options
      |
      v
Deterministic Policy and Risk Gate
      |
      v
Immutable Human Approval
      |
      v
Deterministic Executor
      |
      v
Deterministic Verification
      |
      v
AI Recovery and Handoff Summary
```

## AI and Deterministic Boundary

### AI responsibilities

Use AI where interpretation and judgment add value:

- understand an unfamiliar alert
- write an initial incident brief
- generate multiple root-cause hypotheses
- identify evidence that distinguishes those hypotheses
- select the next read-only investigation tool
- propose bounded PromQL or LogQL query intent
- correlate alert timing with deployments and configuration changes
- interpret logs, metrics, Kubernetes events, and historical incidents
- detect and explain conflicting evidence
- revise confidence after observations
- explain why alternatives became less likely
- retrieve and interpret relevant runbooks
- generate multiple remediation options with risks and tradeoffs
- write incident reports, approval summaries, and handoffs
- explain deterministic verification results

### Deterministic responsibilities

Keep safety, authorization, execution, and truth enforcement deterministic:

- webhook authentication
- alert normalization and persistence
- service, namespace, environment, and time-range resolution
- tool permissions and read/write separation
- query validation, timeouts, and result-size limits
- evidence timestamps, source metadata, and storage
- action allowlists and parameter validation
- RBAC and service/environment scope
- immutable approval records and proposal hashes
- execution and rollback
- recovery measurements and stabilization windows
- audit logging, rate limits, and investigation budgets

Practical rule:

> AI reasons about the environment. Deterministic code controls the environment.

## Phase 1: Real Incident Context

### Objective

Stop converting real alerts into canned incident IDs. Preserve and investigate the actual alert.

### IncidentContext

Every alert-created run should contain a structured context similar to:

```json
{
  "incident_id": "generated-id",
  "source": "grafana",
  "alert_name": "CheckoutHighErrorRate",
  "status": "firing",
  "service": "checkout",
  "namespace": "prod",
  "environment": "production",
  "severity": "critical",
  "started_at": "2026-08-22T14:07:00Z",
  "received_at": "2026-08-22T14:08:03Z",
  "summary": "Checkout 5xx rate exceeded 10%",
  "labels": {},
  "annotations": {},
  "dashboard_url": "",
  "raw_alert": {}
}
```

### Requirements

- Store the complete raw webhook unchanged.
- Generate an incident identifier when the source does not provide one.
- Use service, namespace, environment, severity, and time range from alert context.
- Allow previously unseen services and alerts to start investigations.
- Pass the context directly to LangGraph.
- Retain static mock incident files only for demos, tests, and replay evaluation.
- Preserve the current compact demo flow during migration.

### Primary files

- `agent/incident_context.py`
- `agent/state.py`
- `agent/langgraph_agent.py`
- `runtime/alert_sources.py`
- `runtime/api.py`
- `runtime/models.py`
- `runtime/storage.py`

### Completion criteria

- A real Grafana or generic webhook can create an investigation without a matching JSON fixture.
- The raw alert and normalized context are persisted.
- The investigation uses the alert's actual service, namespace, and time window.
- Existing compact demo tests continue to pass.

## Phase 2: Focused Real Incident Lab

### Objective

Create a reproducible local Kubernetes lab where failures produce real telemetry and real Grafana alerts.

### Lab components

- Kind or k3d Kubernetes cluster
- checkout service
- API service
- payments dependency
- worker service
- Prometheus
- Grafana
- Loki
- traffic generator

### Scenarios

#### A. CrashLoop/OOM

Configure an insufficient memory limit or controlled memory-heavy workload. Produce a real `OOMKilled` state, restart count, memory signal, log sequence, and alert.

#### B. Dependency-driven 503

Stop or delay the payments dependency. Keep API pods healthy while upstream calls fail. Produce 503 metrics, upstream timeout logs, and dependency evidence.

#### C. Deployment-regression 503

Deploy a bad API version or invalid dependency configuration. Ensure the error increase correlates with a real deployment change.

#### D. DNS failure

Configure an invalid internal hostname or controlled name-resolution failure. Produce `NXDOMAIN` or resolver errors without unrelated resource saturation.

#### E. Ambiguous incident

Produce moderate latency and noisy signals without a clear root cause. The expected outcome is a grounded escalation, not a guessed diagnosis.

### Completion criteria

Every scenario must produce a real metric, real logs, a Grafana alert, a real webhook, observable Kubernetes state, and a deterministic reset path.

## Phase 3: AI Hypothesis Investigation Loop

### Objective

Make the model conduct the investigation instead of merely selecting a predefined diagnosis and action.

### Structured investigation decision

Each AI iteration should return:

- situation summary
- two or more current hypotheses when appropriate
- confidence per hypothesis
- supporting evidence IDs
- contradicting evidence IDs
- missing evidence
- next read-only tool and reason, or a finish/escalate decision

Example shape:

```json
{
  "situation_summary": "Checkout is returning elevated 503 responses.",
  "hypotheses": [
    {
      "id": "H1",
      "cause": "Payments dependency unavailable",
      "confidence": 0.55,
      "supporting_evidence_ids": ["E2"],
      "contradicting_evidence_ids": [],
      "evidence_needed": ["payments status", "recent API deployment"]
    }
  ],
  "decision": {
    "type": "call_tool",
    "tool": "get_recent_deploys",
    "reason": "Deployment timing distinguishes dependency failure from regression."
  }
}
```

### Investigation cycle

1. Read the alert and existing evidence.
2. Generate or revise hypotheses.
3. Identify the most discriminating missing evidence.
4. Call one read-only tool.
5. Store the result in the evidence ledger.
6. Update hypotheses and confidence.
7. Continue, finish, or escalate.

### Deterministic budgets

- maximum 6 to 8 read-tool calls
- maximum investigation time
- maximum model-token budget
- no duplicate identical queries
- no write tools during investigation
- tool output size limits
- bounded retry behavior
- escalation when essential evidence is unavailable

### Evidence gate

A diagnosis may be confirmed only when:

- the leading hypothesis passes a configured confidence threshold
- it has at least two independent evidence sources
- it has sufficient margin over the second hypothesis
- no unresolved critical contradiction remains
- critical factual claims have evidence citations

The AI proposes completion. Deterministic code decides whether the evidence gate passes.

### Primary files

- `agent/investigator.py`
- `agent/investigation_models.py`
- `agent/investigation_prompts.py`
- `agent/evidence_ledger.py`
- `agent/langgraph_agent.py`
- `agent/state.py`

## Phase 4: Production-Shaped Read Tools

### Required tools

- `get_pod_status`
- `describe_pod`
- `get_deployment`
- `get_kubernetes_events`
- `query_prometheus_range`
- `query_loki_range`
- `get_recent_deploys`
- `get_git_diff`
- `get_service_dependencies`
- `get_service_owner`
- `search_runbooks`
- `search_previous_incidents`

### Standard evidence envelope

Every tool result must include a stable evidence ID, tool and source, observation timestamp, queried time range, freshness, source reliability, result status, bounded payload, and error details when applicable.

### Query safety

AI may propose query intent, but deterministic validation enforces read-only access, allowed sources, maximum time range and result size, required service and namespace scope, timeouts, and tenant/environment boundaries.

## Phase 5: Evidence Ledger and Grounded Reports

### Objective

Make stored tool evidence the source of truth and make unsupported report claims visible.

### Evidence ledger

Store the raw alert observation, every tool query and result, timestamps, freshness, reliability, hypothesis relationships, contradictions, errors, and missing sources.

### Claim-level validation

After generating a report:

1. split it into factual claims
2. map each claim to evidence IDs
3. classify each as supported, partially supported, unsupported, or contradicted
4. calculate groundedness
5. block remediation when critical diagnosis claims are unsupported

### Report structure

- incident summary and impact
- most likely root cause
- confidence and stopping reason
- supporting and contradicting evidence
- alternatives considered
- recent-change timeline
- remediation options
- recommendation and risk
- unanswered questions
- escalation or handoff information

## Phase 6: Remediation Advice and Immutable Approval

### AI remediation advisor

For each option, provide the structured action, expected effect, risk, reversibility, preconditions, verification plan, rollback plan, and explanation of why it fits the evidence.

### Initial executable scope

Implement one complete production-style action:

- roll back a recent Kubernetes deployment through an approved GitOps proposal

An optional second action, after the first is complete, is scaling a deployment from N to M replicas. Other recommendations may remain advisory or escalation-only.

### Immutable approval artifact

An approval must bind to the proposal ID, run ID, diagnosis, evidence snapshot hash, exact action and parameters, service, namespace, environment, expected effect, risk, verification plan, rollback plan, expiration, proposal hash, approver, and approval time.

The executor verifies this artifact and executes it without rerunning or replanning the investigation.

## Phase 7: Deterministic Verification

Verification should measure rollout completion, ready replicas, error rate over a stabilization window, latency relative to baseline, disappearance of target errors, new Kubernetes events, and Grafana alert state.

Allowed outcomes:

- `resolved`
- `improved`
- `unchanged`
- `regressed`
- `inconclusive`
- `verification_timeout`

AI may explain the result only after deterministic measurements produce the outcome.

## Phase 8: Service Memory and Learning

Store validated diagnoses, useful evidence patterns, successful and unsuccessful actions, relevant deployments, ownership and dependencies, verification outcomes, operator feedback, and follow-up work.

Historical incidents are priors only. Current telemetry must still confirm the diagnosis.

## Phase 9: Evaluation

Build approximately 20 difficult cases from the focused incident families. Include missing or stale telemetry, unrelated deployments, conflicting signals, false-positive and duplicate alerts, tool timeouts, partial recovery, shared symptoms with different causes, unsafe remediation temptations, and unknown incidents.

Compare:

1. deterministic baseline
2. LLM without retrieval
3. LLM with runbook RAG
4. LLM with RAG, tools, and evidence validation

Measure root-cause accuracy, hypothesis recall, next-tool quality, retrieval precision, evidence coverage, factual groundedness, unsupported claims, escalation quality, unsafe action rate, approval-plan consistency, verification accuracy, latency, tool calls, and token use.

## Phase 10: Operator UI

Keep four primary workflow pages:

1. **Incident**: alert, impact, current status, and timeline
2. **Investigation**: hypotheses, evidence, confidence changes, and contradictions
3. **Approval**: exact proposal, risk, hash, rollback, and verification plan
4. **Verification**: before/after signals, execution result, and recovery outcome

Keep raw JSON, retrieval internals, platform health, and tool payloads behind debugging expanders or secondary pages. An engineer should understand the current incident and recommended next step in under 30 seconds.

## Implementation Order

### Milestone 1: Real incident input

- implement `IncidentContext`
- preserve actual webhook payloads
- remove fixture dependency from webhook investigations
- persist source context and time range

### Milestone 2: Real local lab

- deploy the focused Kubernetes and observability stack
- implement reproducible faults and resets
- confirm real alert-to-agent flow

### Milestone 3: AI investigator

- structured hypotheses
- adaptive read-tool selection
- evidence ledger
- confidence revision and stopping rules

### Milestone 4: Grounded reports

- evidence-cited report
- contradiction and alternative explanations
- remediation options
- claim-level groundedness validation

### Milestone 5: Safe action

- immutable proposal and approval hash
- one exact rollback path
- independent deterministic verification

### Milestone 6: Evaluation and presentation

- difficult replay suite
- configuration comparison
- metrics and charts
- concise README and architecture diagram
- reproducible five-minute demonstration
- honest resume bullets

## Explicit Non-Goals

Do not build these before the definition of done is satisfied:

- more than four diagnosable incident classes
- a large collection of independent AI sub-agents
- a general chat platform
- mobile UI
- multiple cloud-provider implementations
- Datadog, Splunk, ServiceNow, and PagerDuty integrations
- automatic unapproved production changes
- model fine-tuning
- a large vector database
- multi-region deployment
- a full autonomous Kubernetes operator

## Definition of Done

The project is complete when this demonstration is reproducible:

1. A real failure is injected into the local Kubernetes environment.
2. Prometheus observes the failure.
3. Grafana fires a real alert.
4. The webhook creates a real persisted incident context.
5. The AI generates multiple hypotheses.
6. The AI calls real read-only tools.
7. New evidence changes the hypothesis ranking.
8. The report explains what happened and why.
9. Every critical factual claim links to evidence.
10. The agent proposes multiple remedies with risks and tradeoffs.
11. A human approves one exact immutable proposal.
12. The executor performs exactly that action without replanning.
13. Verification observes recovery over a stabilization window.
14. The complete investigation, approval, execution, and verification are auditable.
15. The ambiguous scenario escalates without guessing.
16. Evaluation shows the hybrid agent outperforming at least one simpler baseline without increasing unsafe execution.

## Resume Outcome

The finished project should support an honest statement such as:

> Built an event-driven AI SRE agent triggered by Grafana alerts that investigates Kubernetes incidents using Prometheus metrics, Loki logs, deployment context, runbooks, and infrastructure state; generates evidence-cited root-cause hypotheses and remediation options; and executes only immutable, explicitly approved actions through a deterministic safety layer with rollback and recovery verification.

## Implementation Status

Last updated: 2026-08-23

| Phase | Status | Implemented | Remaining proof |
|---|---|---|---|
| 1. Real incident context | Complete | Provider-neutral context, raw Grafana payload persistence, stable IDs, arbitrary external alerts, graph and queue propagation | A separately hosted Grafana deployment is an external-integration extension, not an MVP blocker |
| 2. Real incident lab | Complete | Reproducible Kind/Helm stack, five Grafana-managed scenario rules, stable Kind-network Prometheus/Loki endpoints, fault/reset controls, and five audited end-to-end scenario contracts | Further reduce the optional monitoring footprint for 4 GB Docker VMs |
| 3. AI investigation | Complete for MVP scope | Structured multi-hypothesis model output, missing/supporting/contradicting evidence, bounded tool loop, deterministic evidence gate, and recorded model/fallback provenance | Improve small-model structured-output reliability without weakening the guardrail |
| 4. Read tools | Complete for MVP scope | Kubernetes workload/events, alert-time-bounded Prometheus and log queries, deploy/Git diff, dependency, owner, dashboard, runbook and incident-history adapters, standardized evidence envelopes, partial-backend degradation, and prerequisite routing | Tempo remains optional and was not needed by the five-case demo |
| 5. Grounded reports | Complete for MVP scope | Evidence ledger, stable IDs, independent-source claim enforcement, contradiction blocking, groundedness score, cited report structure, and remediation tradeoffs | Expand the factual-grounding benchmark beyond the portfolio scenario set |
| 6. Safe approval | Complete for MVP scope | Immutable proposal/evidence hashes, expiry, execution without replanning, exact rollback metadata including zero replicas, isolated write gateway, and GitOps rollback artifacts | Real Git provider merge and continuous-delivery rollout are intentionally outside this small-project scope |
| 7. Verification | Complete for MVP scope | Deterministic checks, normalized outcomes, repeated stabilization samples, Service503 recovery proof, and honest GitOps `inconclusive` handling when rollout is unobserved | Tune windows against additional production-shaped workloads |
| 8. Learning | Complete for MVP scope | Validated diagnosis/action/verification records, run-linked operator feedback and audit events, with only high-rating confirmed feedback used as a bounded low-weight prior | Validate usefulness with several external human reviewers |
| 9. Evaluation | Complete for portfolio scope | Published 20-replay comparison plus five real Kind/Grafana scenario contracts; the safety scope and model fallback coverage are explicit | Broader production safety and reliability claims require a larger, independently reviewed dataset |
| 10. Operator UI | Complete for MVP scope | Four workflow stages, hypotheses, evidence ledger/gate, decision-focused immutable approval, normalized verification, honest handoff, and secondary debug surfaces | Conduct a short external usability test and record findings |

### Real lab evidence recorded on 2026-08-23 Europe/Berlin

- The audited result is `evaluation/live_scenario_results.json`: all five Grafana/Kind contracts passed, each report had groundedness `1.0`, and the audit found zero unsafe scenario actions.
- OOM run `run_8e78deb7df` confirmed `CrashLoopBackOff` from Kubernetes state and OOM termination evidence, proposed no unjustified restart/scale action, and handed off safely.
- Dependency run `run_b7883371b1` confirmed `Service503`, used the model with deterministic guardrails, revised its hypothesis from `Unknown` after real tool evidence, proposed exactly `payments` replicas `0 -> 1`, preserved an exact `1 -> 0` rollback, executed the approved hash unchanged, and resolved over four stabilization samples as error rate fell from `58.70%` to `0%`. Its audit also records Grafana's resolved notification.
- Deployment-regression run `run_2b8e36d636` confirmed the bad release and created the exact approved GitOps rollback artifact. It correctly reported `inconclusive` because this local project does not merge and observe a continuous-delivery rollout.
- DNS run `run_c3bed86b9e` confirmed repeated NXDOMAIN evidence but offered no unsafe CoreDNS mutation; it escalated for a human-owned remediation.
- Ambiguous run `run_711eb08e2f` used the model, returned `Unknown` at low confidence, failed the evidence gate, created no proposal, and escalated instead of guessing.
- The 20-case replay matrix in `evaluation/benchmark_matrix_results.json` records hybrid type accuracy `1.0` with `90%` model coverage versus LLM-only accuracy `0.2`; its execution-safety result is explicitly preview-policy evidence, not a broad production-safety claim.
- Earlier run `run_9144b76fed` is deliberately excluded: restarting API pods reset in-memory fault state while the dependency remained unavailable, so it did not prove causal recovery.
- Final repository verification: `76 passed, 3 skipped`; Python compilation, shell syntax, JSON validation, Compose configuration, and `git diff --check` passed. Fresh API, worker, and executor images built successfully; the API image is approximately `298 MB`.

“Complete” means the focused, resume-sized MVP and its documented local proof are present. It does not mean unattended production autonomy, broad production safety, external GitOps delivery, or validation against every observability stack.

## Definition-of-Done Audit

Audited on 2026-08-23 against current files, stored run records, execution/audit events, evaluation artifacts, and the complete automated suite:

| # | Requirement | Result | Authoritative evidence |
|---|---|---|---|
| 1 | Inject a real local Kubernetes failure | Pass | `lab/scripts/kind_lab_fault.sh`; five stored `KIND-*` runs |
| 2 | Prometheus observes the failure | Pass | Dependency run metric evidence; error rate `58.70%` before repair |
| 3 | Grafana fires a real alert | Pass | `grafana_webhook_source` and Grafana context checks pass for all five runs |
| 4 | Persist a real incident context | Pass | Stored request contains normalized context and complete raw Grafana alert |
| 5 | AI generates multiple hypotheses | Pass | Dependency audit check `multiple_hypotheses`; `planner_backend=ollama_guardrail` |
| 6 | AI calls real read-only tools | Pass | Dependency audit check `real_read_tool_observations`; metrics, pod, logs, and dependency observations are stored |
| 7 | Evidence changes hypothesis ranking | Pass | Dependency trace changes from `Unknown` to `Service503` after tool observations |
| 8 | Explain what happened and why | Pass | Stored incident report, alternatives, contradictions, situation summary, and handoff |
| 9 | Link critical claims to evidence | Pass | Groundedness `1.0`, `critical_claims_supported=true`, stable evidence IDs |
| 10 | Offer multiple remedies and tradeoffs | Pass | Approved bounded action plus advisory escalation, each with risk and verification information |
| 11 | Human approves one immutable proposal | Pass | Approval artifact and proposal hash `bec7092a...` for `run_b7883371b1` |
| 12 | Execute exactly the approved action | Pass | Approved and executed hashes match; only `payments` scaled `0 -> 1`; no replanning |
| 13 | Observe recovery over stabilization | Pass | Four samples across approximately 49 seconds; error rate `58.70% -> 0%`; outcome `resolved` |
| 14 | Keep the workflow auditable | Pass | Required run, plan, approval, execution, persistence, and Grafana-resolution events all recorded |
| 15 | Escalate the ambiguous case | Pass | `run_711eb08e2f`: `Unknown`, evidence gate failed, no proposal or approval |
| 16 | Beat a simpler baseline without extra unsafe execution | Pass for portfolio scope | Hybrid accuracy `1.0` versus LLM-only `0.2`, preview unsafe-rate delta `0`; live audit passes `5/5` with unsafe action count `0` |

The audit intentionally limits item 16 to portfolio evidence. It does not convert a small local benchmark into a general production-safety claim.
