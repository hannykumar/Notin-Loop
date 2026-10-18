#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/common.sh"

kubectl delete deployment oom-worker -n sre-lab --ignore-not-found
kubectl scale deployment payments -n sre-lab --replicas=1
./lab/scripts/kind_lab_gitops_version.sh known-good
kubectl annotate deployment demo-api -n sre-lab sre-agent/release- 2>/dev/null || true
kubectl rollout status deployment/demo-api -n sre-lab --timeout=120s
while IFS= read -r pod; do
  kubectl exec -n sre-lab "${pod}" -- python -c "import urllib.request; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8088/admin/reset', method='POST'), timeout=15).read()"
done < <(kubectl get pods -n sre-lab -l app=demo-api -o name)
kubectl rollout status deployment/payments -n sre-lab --timeout=120s
