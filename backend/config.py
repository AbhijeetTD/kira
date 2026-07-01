from __future__ import annotations

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Ollama ────────────────────────────────────────────────────────────────
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "llama3.2"

    # ── Kubernetes ────────────────────────────────────────────────────────────
    kubeconfig_path: Optional[str] = None
    kube_context: Optional[str] = None          # e.g. "kind-dev-cluster"
    default_namespace: str = "default"
    approval_mode: bool = True
    auto_approve_threshold: int = 90            # confidence >= 90% → auto-fix
    public_url: str = "http://localhost:8000"

    # ── Notifications (optional) ──────────────────────────────────────────────
    teams_webhook_url: Optional[str] = None

    # ── Jira (optional) ───────────────────────────────────────────────────────
    jira_url: Optional[str] = None
    jira_email: Optional[str] = None
    jira_api_token: Optional[str] = None
    jira_project_key: str = "KS"
    jira_issue_type: str = "Task"
    jira_enabled: bool = False

    # ── Memory monitor ────────────────────────────────────────────────────────
    memory_monitor_enabled: bool = False
    memory_monitor_interval_s: int = 30
    memory_monitor_threshold_pct: int = 80
    memory_monitor_namespaces: str = ""


settings = Settings()
