# Runbook: Deployment Regression After Recent Release

## Metadata
- incident_type: DeploymentRegression
- primary_signal: regression after deploy
- common_root_causes: bad config, bad image, feature flag regression
- severity: SEV1
- services: api, checkout, worker, ingress

## Symptoms
- Errors or latency start within minutes of a deploy
- Rollback often restores service quickly
- Logs may vary by service but timing is consistent

## Fast Checks (2 minutes)
1. Check recent deploy history
2. Compare alert start time to deploy time
3. Check traces, logs, and dashboards for regressions introduced by the rollout

## Evidence to Collect
- Recent deploy list
- Error/latency charts
- Incident history for the service
- Relevant logs and trace spans

## Diagnosis Rules
- Latest deploy within 30 minutes of the incident -> strong regression candidate
- Same symptom resolved by rollback previously -> stronger confidence
- No infrastructure-level signals -> application rollout is more likely

## Mitigation (Safe)
- Restart the worst pod only as a stopgap
- Prefer rollback or GitOps reversion through the human approval path

## Permanent Fix
- Strengthen canaries and rollback automation
- Add deploy correlation to alerts
