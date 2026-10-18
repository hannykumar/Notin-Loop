#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/common.sh"

kubeconfig_path="${SRE_LAB_KUBECONFIG:-$PWD/.env.lab-kubeconfig}"

kind get kubeconfig --internal --name "${cluster_name}" >"${kubeconfig_path}"
chmod 600 "${kubeconfig_path}"

KUBECONFIG="${kubeconfig_path}" \
KUBECTL_CONTEXT="kind-${cluster_name}" \
PROMETHEUS_BASE_URL="http://${cluster_name}-control-plane:30900" \
LOKI_BASE_URL="http://${cluster_name}-control-plane:30100" \
PROMETHEUS_HTTP_REQUESTS_METRIC="demo_http_requests_total" \
PROMETHEUS_RATE_WINDOW="${PROMETHEUS_RATE_WINDOW:-1m}" \
SRE_VERIFICATION_STABILIZATION_SECONDS="${SRE_VERIFICATION_STABILIZATION_SECONDS:-70}" \
SRE_VERIFICATION_SAMPLE_INTERVAL_SECONDS="${SRE_VERIFICATION_SAMPLE_INTERVAL_SECONDS:-5}" \
SRE_LOGS_BACKEND="kubectl" \
SRE_TOOL_TIMEOUT_SECONDS="${SRE_TOOL_TIMEOUT_SECONDS:-60}" \
SRE_TOOL_MAX_RETRIES="${SRE_TOOL_MAX_RETRIES:-1}" \
SRE_AGENT_PLANNER_MODEL="${SRE_AGENT_PLANNER_MODEL:-llama3.2:1b}" \
SRE_AGENT_PLANNER_TIMEOUT_SECONDS="${SRE_AGENT_PLANNER_TIMEOUT_SECONDS:-90}" \
docker compose -f docker-compose.yml -f docker-compose.live.yml up -d --build kubectl-mcp executor worker sre-agent

printf 'Live SRE agent is connected to Kind. Kubeconfig: %s\n' "${kubeconfig_path}"
