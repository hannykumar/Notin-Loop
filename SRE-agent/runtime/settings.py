from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    api_token: str
    api_tokens: str
    grafana_webhook_token: str
    alert_webhook_token: str
    webhook_wait_for_plan: bool
    mcp_backend: str
    kubectl_binary: str
    kubeconfig_path: str
    kubectl_context: str
    prometheus_base_url: str
    loki_base_url: str
    logs_backend: str
    gitops_repo_dir: str
    gitops_base_branch: str
    gitops_author: str
    executor_base_url: str
    executor_shared_token: str
    request_timeout_seconds: float
    investigation_timeout_seconds: float
    execution_request_timeout_seconds: float
    tool_timeout_seconds: float
    executor_timeout_seconds: float
    tool_max_retries: int
    executor_max_retries: int
    rate_limit_requests: int
    rate_limit_window_seconds: int
    planner_provider: str
    planner_model: str
    planner_base_url: str
    planner_api_key: str
    planner_timeout_seconds: float
    planner_probe_timeout_seconds: float
    planner_probe_ttl_seconds: float
    planner_failure_cooldown_seconds: float
    planner_max_retries: int
    planner_temperature: float
    planner_context_tokens: int
    planner_output_tokens: int
    demo_service_api_url: str
    grafana_api_url: str
    prometheus_api_url: str
    grafana_username: str
    grafana_password: str
    grafana_active_run_window_seconds: float
    alert_verify_timeout_seconds: float
    alert_verify_poll_seconds: float
    verification_stabilization_seconds: float
    verification_sample_interval_seconds: float
    async_queue_enabled: bool
    queue_backend: str
    max_async_workers: int
    queue_name: str
    queue_group_ttl_seconds: int
    queue_pop_timeout_seconds: int
    live_eval_service: str
    live_eval_namespace: str
    retrieval_limit: int
    retrieval_retry_limit: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///./runtime.db"),
        redis_url=os.getenv("REDIS_URL", ""),
        api_token=os.getenv("OPS_API_TOKEN", "").strip(),
        api_tokens=os.getenv("OPS_API_TOKENS", "").strip(),
        grafana_webhook_token=os.getenv("GRAFANA_WEBHOOK_TOKEN", "").strip(),
        alert_webhook_token=os.getenv("ALERT_WEBHOOK_TOKEN", os.getenv("GRAFANA_WEBHOOK_TOKEN", "")).strip(),
        webhook_wait_for_plan=os.getenv("SRE_WEBHOOK_WAIT_FOR_PLAN", "0").strip().lower() in {"1", "true", "yes"},
        mcp_backend=os.getenv("SRE_MCP_BACKEND", "mock").strip().lower(),
        kubectl_binary=os.getenv("KUBECTL_BINARY", "kubectl").strip(),
        kubeconfig_path=os.getenv("KUBECONFIG", "").strip(),
        kubectl_context=os.getenv("KUBECTL_CONTEXT", "").strip(),
        prometheus_base_url=os.getenv("PROMETHEUS_BASE_URL", "").strip(),
        loki_base_url=os.getenv("LOKI_BASE_URL", "").strip(),
        logs_backend=os.getenv("SRE_LOGS_BACKEND", "kubectl").strip().lower(),
        gitops_repo_dir=os.getenv("GITOPS_REPO_DIR", "data/gitops_repo").strip(),
        gitops_base_branch=os.getenv("GITOPS_BASE_BRANCH", "main").strip(),
        gitops_author=os.getenv("GITOPS_AUTHOR", "sre-agent").strip(),
        executor_base_url=os.getenv("EXECUTOR_BASE_URL", "").strip(),
        executor_shared_token=os.getenv("EXECUTOR_SHARED_TOKEN", "").strip(),
        request_timeout_seconds=float(os.getenv("SRE_REQUEST_TIMEOUT_SECONDS", "30")),
        investigation_timeout_seconds=float(os.getenv("SRE_INVESTIGATION_TIMEOUT_SECONDS", "600")),
        execution_request_timeout_seconds=float(os.getenv("SRE_EXECUTION_REQUEST_TIMEOUT_SECONDS", "120")),
        tool_timeout_seconds=float(os.getenv("SRE_TOOL_TIMEOUT_SECONDS", "15")),
        executor_timeout_seconds=float(os.getenv("EXECUTOR_TIMEOUT_SECONDS", "120")),
        tool_max_retries=int(os.getenv("SRE_TOOL_MAX_RETRIES", "2")),
        executor_max_retries=int(os.getenv("EXECUTOR_MAX_RETRIES", "2")),
        rate_limit_requests=int(os.getenv("SRE_RATE_LIMIT_REQUESTS", "30")),
        rate_limit_window_seconds=int(os.getenv("SRE_RATE_LIMIT_WINDOW_SECONDS", "60")),
        planner_provider=os.getenv("SRE_AGENT_PLANNER_PROVIDER", "deterministic").strip().lower(),
        planner_model=os.getenv("SRE_AGENT_PLANNER_MODEL", "qwen3:4b").strip(),
        planner_base_url=os.getenv("SRE_AGENT_PLANNER_BASE_URL", "http://127.0.0.1:11434").strip(),
        planner_api_key=os.getenv("SRE_AGENT_PLANNER_API_KEY", "").strip(),
        planner_timeout_seconds=float(os.getenv("SRE_AGENT_PLANNER_TIMEOUT_SECONDS", "30")),
        planner_probe_timeout_seconds=float(os.getenv("SRE_AGENT_PLANNER_PROBE_TIMEOUT_SECONDS", "2.5")),
        planner_probe_ttl_seconds=float(os.getenv("SRE_AGENT_PLANNER_PROBE_TTL_SECONDS", "20")),
        planner_failure_cooldown_seconds=float(os.getenv("SRE_AGENT_PLANNER_FAILURE_COOLDOWN_SECONDS", "30")),
        planner_max_retries=int(os.getenv("SRE_AGENT_PLANNER_MAX_RETRIES", "1")),
        planner_temperature=float(os.getenv("SRE_AGENT_PLANNER_TEMPERATURE", "0.1")),
        planner_context_tokens=int(os.getenv("SRE_AGENT_CONTEXT_TOKENS", "8192")),
        planner_output_tokens=int(os.getenv("SRE_AGENT_OUTPUT_TOKENS", "1024")),
        demo_service_api_url=os.getenv("SRE_DEMO_SERVICE_API_URL", "http://127.0.0.1:8088").strip(),
        grafana_api_url=os.getenv("SRE_GRAFANA_API_URL", "http://127.0.0.1:3000").strip(),
        prometheus_api_url=os.getenv("SRE_PROMETHEUS_API_URL", "http://127.0.0.1:9090").strip(),
        grafana_username=os.getenv("SRE_GRAFANA_USERNAME", "admin").strip(),
        grafana_password=os.getenv("SRE_GRAFANA_PASSWORD", "admin").strip(),
        grafana_active_run_window_seconds=float(os.getenv("SRE_GRAFANA_ACTIVE_RUN_WINDOW_SECONDS", "900")),
        alert_verify_timeout_seconds=float(os.getenv("SRE_ALERT_VERIFY_TIMEOUT_SECONDS", "45")),
        alert_verify_poll_seconds=float(os.getenv("SRE_ALERT_VERIFY_POLL_SECONDS", "5")),
        verification_stabilization_seconds=float(os.getenv("SRE_VERIFICATION_STABILIZATION_SECONDS", "0")),
        verification_sample_interval_seconds=float(os.getenv("SRE_VERIFICATION_SAMPLE_INTERVAL_SECONDS", "5")),
        async_queue_enabled=os.getenv("SRE_ASYNC_QUEUE_ENABLED", "1").strip().lower() in {"1", "true", "yes"},
        queue_backend=os.getenv("SRE_QUEUE_BACKEND", "inprocess").strip().lower(),
        max_async_workers=int(os.getenv("SRE_MAX_ASYNC_WORKERS", "2")),
        queue_name=os.getenv("SRE_QUEUE_NAME", "sre_agent_jobs").strip(),
        queue_group_ttl_seconds=int(os.getenv("SRE_QUEUE_GROUP_TTL_SECONDS", "900")),
        queue_pop_timeout_seconds=int(os.getenv("SRE_QUEUE_POP_TIMEOUT_SECONDS", "5")),
        live_eval_service=os.getenv("SRE_LIVE_EVAL_SERVICE", "api").strip(),
        live_eval_namespace=os.getenv("SRE_LIVE_EVAL_NAMESPACE", "prod").strip(),
        retrieval_limit=int(os.getenv("SRE_RETRIEVAL_LIMIT", "8")),
        retrieval_retry_limit=int(os.getenv("SRE_RETRIEVAL_RETRY_LIMIT", "1")),
    )


def reset_settings_cache() -> None:
    get_settings.cache_clear()
