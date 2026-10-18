#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ -x .venv/bin/python ]]; then
  export PATH="$PWD/.venv/bin:$PATH"
fi

if [[ "${1:-}" == "--llm" ]]; then
  export SRE_AGENT_PLANNER_PROVIDER=ollama
  export SRE_AGENT_PLANNER_MODEL="${SRE_AGENT_PLANNER_MODEL:-llama3.2:1b}"
  export SRE_AGENT_PLANNER_TIMEOUT_SECONDS="${SRE_AGENT_PLANNER_TIMEOUT_SECONDS:-90}"
  export SRE_AGENT_PLANNER_MAX_RETRIES="${SRE_AGENT_PLANNER_MAX_RETRIES:-0}"
  export SRE_MAX_ASYNC_WORKERS=1
elif [[ -n "${1:-}" ]]; then
  printf 'Usage: bash scripts/start_stack.sh [--llm]\n' >&2
  exit 2
fi

export DATABASE_URL="${DATABASE_URL:-sqlite:///./runtime.db}"
export EXECUTOR_BASE_URL="${EXECUTOR_BASE_URL:-http://127.0.0.1:8091}"
export OPS_API_TOKENS="${OPS_API_TOKENS:-}"
export SRE_DEFAULT_API_TOKEN="${SRE_DEFAULT_API_TOKEN:-}"
export SRE_DEFAULT_TOOL_TRANSPORT="${SRE_DEFAULT_TOOL_TRANSPORT:-mcp}"
export SRE_MCP_BACKEND="${SRE_MCP_BACKEND:-mock}"
export GRAFANA_WEBHOOK_TOKEN="${GRAFANA_WEBHOOK_TOKEN:-}"
export GITOPS_REPO_DIR="${GITOPS_REPO_DIR:-data/gitops_repo}"

# Start a local executor only when the configured executor points back to this machine.
EXECUTOR_PID=""
if [[ "$EXECUTOR_BASE_URL" == "http://127.0.0.1:8091" || "$EXECUTOR_BASE_URL" == "http://localhost:8091" ]]; then
  PYTHONPATH=. uvicorn executor.api:app --host "${SRE_BIND_HOST:-127.0.0.1}" --port 8091 &
  EXECUTOR_PID=$!
fi

PYTHONPATH=. uvicorn runtime.api:app --host "${SRE_BIND_HOST:-127.0.0.1}" --port 8090 &
API_PID=$!

cleanup() {
  if [[ -n "$EXECUTOR_PID" ]]; then
    kill "$EXECUTOR_PID" >/dev/null 2>&1 || true
  fi
  kill "$API_PID" >/dev/null 2>&1 || true
}

trap cleanup EXIT

printf 'SRE Copilot: http://localhost:8090\n'
wait "$API_PID"
