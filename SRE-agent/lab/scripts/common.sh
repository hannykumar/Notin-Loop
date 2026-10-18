#!/usr/bin/env bash
# Shared scope for disposable Kind lab commands; never use the current context.
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
cluster_name="${SRE_LAB_CLUSTER_NAME:-sre-agent-lab}"
lab_context="kind-${cluster_name}"
kubectl() { command kubectl --context "${lab_context}" "$@"; }
helm() { command helm --kube-context "${lab_context}" "$@"; }
