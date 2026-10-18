#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/common.sh"

fault_name="${1:-}"
set_demo_mode() {
  local mode="$1"
  local pod
  kubectl rollout status deployment/demo-api -n sre-lab --timeout=120s
  while IFS= read -r pod; do
    kubectl exec -n sre-lab "${pod}" -- python -c "import urllib.request; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8088/admin/mode/${mode}', method='POST'), timeout=15).read()"
  done < <(kubectl get pods -n sre-lab -l app=demo-api -o name)
}

case "$fault_name" in
  oom)
    kubectl apply -f lab/k8s/fault-oom.yaml
    ;;
  dependency-503)
    kubectl scale deployment payments -n sre-lab --replicas=0
    set_demo_mode dependency503
    ;;
  deployment-regression)
    ./lab/scripts/kind_lab_gitops_version.sh bad-release
    kubectl annotate deployment demo-api -n sre-lab sre-agent/release=bad-release --overwrite
    set_demo_mode deployment_regression
    ;;
  dns)
    set_demo_mode dnsfailure
    ;;
  ambiguous)
    set_demo_mode ambiguous
    ;;
  *)
    echo "usage: $0 {oom|dependency-503|deployment-regression|dns|ambiguous}" >&2
    exit 2
    ;;
esac
