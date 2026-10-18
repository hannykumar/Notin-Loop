# Runbook: CrashLoopBackOff due to OOMKilled

## Metadata
- incident_type: CrashLoopBackOff
- primary_signal: container restarts
- common_root_causes: OOMKilled, bad config, missing secrets
- severity: SEV2
- services: any

## Symptoms
- Pods show `CrashLoopBackOff`
- Restarts increasing rapidly
- App endpoints intermittently fail

## Fast Checks (2 minutes)
1. Check pod status and restarts (look for high restart count)
2. Describe pod (look for `Reason: OOMKilled`)
3. Check memory usage (spikes near limits)

## Evidence to Collect
- Pod name, namespace, restart count
- Last termination reason
- Last 200 log lines before crash
- Memory limits/requests

## Diagnosis Rules
- If `describe pod` shows `Reason: OOMKilled` → memory limit too low or leak
- If logs show big in-memory processing → suspect leak / large payload
- If not OOMKilled → check config/secrets

## Mitigation (Safe)
- Scale replicas (reduce load per pod)
- Temporarily increase memory limit (if allowed)
- Roll back to last known good deploy if recent change

## Permanent Fix
- Fix memory leak / payload handling
- Set correct memory requests/limits
- Add memory profiling and alerts

## Escalation
- Escalate to service owner if repeated OOM within 30 minutes
- If customer-impacting > 15 min → SEV1
