# Runbook: Service Returning 503 / Upstream Dependency Down

## Metadata
- incident_type: Service503
- primary_signal: 503 errors
- common_root_causes: upstream outage, bad routing, pods not ready
- severity: SEV1
- services: any

## Symptoms
- 503 errors spike
- Health checks failing
- Users impacted

## Fast Checks (2 minutes)
1. Check pods readiness
2. Check ingress/load balancer
3. Check logs for upstream timeout/connection failures

## Evidence to Collect
- 503 rate + timeframe
- Pod readiness status
- Last 200 log lines
- Recent deploy/config changes

## Diagnosis Rules
- Pods not ready → readiness probe / startup issue
- Upstream timeouts in logs → dependency incident
- Only one region affected → routing/network issue

## Mitigation (Safe)
- Failover to healthy region/upstream (if available)
- Roll back recent deploy if correlated
- Disable non-critical dependency calls (feature flags)

## Permanent Fix
- Circuit breakers + retries w/ backoff
- Improve readiness probes
- Dependency health monitoring

## Escalation
- SEV1 if customer impact confirmed and > 5 minutes
