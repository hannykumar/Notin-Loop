# Runbook: DNS Resolution Failures in Cluster

## Metadata
- incident_type: DNSFailure
- primary_signal: "could not resolve host"
- common_root_causes: CoreDNS issues, network policy, upstream DNS outage
- severity: SEV2
- services: any

## Symptoms
- Logs show `NXDOMAIN` / `could not resolve`
- Multiple services fail reaching hostnames

## Fast Checks (2 minutes)
1. Confirm blast radius (multiple services?)
2. Check CoreDNS pods health/restarts
3. Check recent network policy/cluster changes

## Evidence to Collect
- DNS error logs
- CoreDNS status
- Cluster events
- Node DNS config if relevant

## Diagnosis Rules
- CoreDNS restarting → CoreDNS issue
- Only one namespace affected → policy issue
- Upstream DNS outage → external dependency

## Mitigation (Safe)
- Restart/scale CoreDNS (if allowed)
- Temporary workaround for critical paths (if feasible)

## Permanent Fix
- Resource sizing for CoreDNS
- DNS health checks + alerts
- Review network policies

## Escalation
- Escalate if multi-service impact or > 10 minutes persistent
