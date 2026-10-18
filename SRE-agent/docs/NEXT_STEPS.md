# Next Steps

`NORTH_STAR.md` records the original portfolio scope and historical evidence. The focused September rebuild is complete; [REBUILD.md](REBUILD.md) is the current status. The list below describes the earlier MVP and is not a fresh certification of the replacement UI or production readiness.

## Completed for the focused MVP

- Provider-neutral alert context with raw Grafana payload persistence and deduplication.
- Five real Kind/Grafana scenario contracts: OOM, dependency 503, deployment regression, DNS, and ambiguous escalation.
- AI-generated competing hypotheses, bounded next-tool selection, weak-query rewriting, and explicit uncertainty.
- Real Kubernetes, Prometheus, log, dependency, deploy, runbook, owner, dashboard, and incident-history reads with evidence envelopes.
- Alert-time-bounded logs, contradiction tracking, evidence freshness, independent-source gating, claim citations, and groundedness scoring.
- Structured catalog actions, immutable proposal/evidence hashes, expiry, RBAC, an isolated write gateway, exact execution without replanning, and rollback metadata.
- Repeated deterministic verification with `resolved`, `improved`, `unchanged`, `regressed`, `inconclusive`, and `verification_timeout` outcomes.
- Feedback-backed service memory that only uses confirmed, highly rated outcomes as a bounded low-weight prior.
- A 20-case replay comparison and a five-run real-execution scenario audit.
- Workflow-first UI for incident brief, investigation, approval, verification, handoff, and progressive disclosure.

## Optional follow-on work

- Run a short usability session with two or three SRE/DevOps reviewers and record concrete findings.
- Connect a real Git provider and continuous-delivery controller so GitOps merge and rollout can be observed instead of reported as `inconclusive`.
- Validate against a separately hosted Grafana and additional Kubernetes distributions.
- Add Tempo only when a trace-driven scenario justifies it.
- Expand the independently reviewed evaluation dataset before making broad production-safety claims.
- Improve small local model schema reliability while keeping deterministic evidence and execution boundaries unchanged.
- Reduce the optional monitoring footprint further for 4 GB Docker environments.

## Boundary

AI is responsible for hypothesis generation, evidence requests, query rewriting, explanations, and summaries. Deterministic code remains responsible for evidence validation, policy, approval integrity, action rendering, execution, rollback, verification, and audit. Unattended remediation, arbitrary shell execution, and a general AIOps platform remain outside the project scope.
