#!/usr/bin/env python3
"""settings_routes.py — Settings API routes for KIRA.

GET  /settings               Return current settings (tokens masked)
POST /settings               Update settings → write to .env, hot-reload
GET  /settings/kube-contexts List available kubectl contexts
GET  /settings/ollama-models List locally available Ollama models
POST /settings/test/jira     Test Jira connectivity
POST /settings/test/teams    Test Teams connectivity
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.config import settings

router = APIRouter(prefix="/settings", tags=["settings"])

# Path to the .env file (relative to project root)
_ENV_PATH = Path(".env")

# Mask placeholder shown in the UI for already-set secrets
_MASK = "••••••"

# Fields that are secrets and should be masked in GET response
_SECRET_FIELDS = {"jira_api_token", "teams_webhook_url"}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _read_env_file() -> dict[str, str]:
    """Read .env into a key→value dict (ignores comments & blanks)."""
    env: dict[str, str] = {}
    if not _ENV_PATH.exists():
        return env
    for line in _ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def _write_env_file(overrides: dict[str, str]) -> None:
    """Merge overrides into .env, preserving comments and ordering."""
    lines: list[str] = []
    seen: set[str] = set()

    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                lines.append(line)
                continue
            if "=" in stripped:
                k, _, _ = stripped.partition("=")
                k = k.strip()
                seen.add(k)
                env_key = k.upper()
                if env_key in overrides:
                    lines.append(f"{env_key}={overrides[env_key]}")
                else:
                    lines.append(line)
            else:
                lines.append(line)

    # Append any new keys not already in the file
    for k, v in overrides.items():
        if k not in seen and k.upper() not in seen:
            lines.append(f"{k}={v}")

    _ENV_PATH.write_text("\n".join(lines) + "\n")


def _hot_reload(overrides: dict[str, str]) -> None:
    """Apply overrides directly to the live settings object (no restart needed)."""
    mapping = {
        "OLLAMA_BASE_URL": "ollama_base_url",
        "OLLAMA_MODEL": "ollama_model",
        "KUBE_CONTEXT": "kube_context",
        "DEFAULT_NAMESPACE": "default_namespace",
        "APPROVAL_MODE": "approval_mode",
        "AUTO_APPROVE_THRESHOLD": "auto_approve_threshold",
        "TEAMS_WEBHOOK_URL": "teams_webhook_url",
        "JIRA_ENABLED": "jira_enabled",
        "JIRA_URL": "jira_url",
        "JIRA_EMAIL": "jira_email",
        "JIRA_API_TOKEN": "jira_api_token",
        "JIRA_PROJECT_KEY": "jira_project_key",
        "JIRA_ISSUE_TYPE": "jira_issue_type",
    }
    for env_key, attr in mapping.items():
        if env_key not in overrides:
            continue
        raw = overrides[env_key]
        # Coerce types
        if attr in ("approval_mode", "jira_enabled"):
            object.__setattr__(settings, attr, raw.lower() in ("true", "1", "yes"))
        elif attr == "auto_approve_threshold":
            try:
                object.__setattr__(settings, attr, int(raw))
            except ValueError:
                pass
        else:
            object.__setattr__(settings, attr, raw or None)


# ── GET /settings ──────────────────────────────────────────────────────────────

@router.get("")
async def get_settings():
    """Return current settings. Secrets are masked with ••••••."""
    env = _read_env_file()

    def _get(key: str, fallback: Any = "") -> Any:
        return env.get(key, str(fallback))

    def _masked(key: str) -> str:
        val = env.get(key, "")
        return _MASK if val else ""

    return {
        # Ollama
        "ollama_base_url": _get("OLLAMA_BASE_URL", settings.ollama_base_url),
        "ollama_model": _get("OLLAMA_MODEL", settings.ollama_model),
        # Kubernetes
        "kube_context": _get("KUBE_CONTEXT", settings.kube_context or ""),
        "default_namespace": _get("DEFAULT_NAMESPACE", settings.default_namespace),
        # Behaviour
        "approval_mode": _get("APPROVAL_MODE", "true" if settings.approval_mode else "false"),
        "auto_approve_threshold": _get("AUTO_APPROVE_THRESHOLD", settings.auto_approve_threshold),
        # Teams
        "teams_webhook_url": _masked("TEAMS_WEBHOOK_URL"),
        # Jira
        "jira_enabled": _get("JIRA_ENABLED", "true" if settings.jira_enabled else "false"),
        "jira_url": _get("JIRA_URL", settings.jira_url or ""),
        "jira_email": _get("JIRA_EMAIL", settings.jira_email or ""),
        "jira_api_token": _masked("JIRA_API_TOKEN"),
        "jira_project_key": _get("JIRA_PROJECT_KEY", settings.jira_project_key),
        "jira_issue_type": _get("JIRA_ISSUE_TYPE", settings.jira_issue_type),
    }


# ── POST /settings ─────────────────────────────────────────────────────────────

class SettingsUpdate(BaseModel):
    ollama_base_url: Optional[str] = None
    ollama_model: Optional[str] = None
    kube_context: Optional[str] = None
    default_namespace: Optional[str] = None
    approval_mode: Optional[str] = None
    auto_approve_threshold: Optional[str] = None
    teams_webhook_url: Optional[str] = None
    jira_enabled: Optional[str] = None
    jira_url: Optional[str] = None
    jira_email: Optional[str] = None
    jira_api_token: Optional[str] = None
    jira_project_key: Optional[str] = None
    jira_issue_type: Optional[str] = None


@router.post("")
async def update_settings(body: SettingsUpdate):
    """Save settings to .env and hot-reload into memory."""
    key_map = {
        "ollama_base_url": "OLLAMA_BASE_URL",
        "ollama_model": "OLLAMA_MODEL",
        "kube_context": "KUBE_CONTEXT",
        "default_namespace": "DEFAULT_NAMESPACE",
        "approval_mode": "APPROVAL_MODE",
        "auto_approve_threshold": "AUTO_APPROVE_THRESHOLD",
        "teams_webhook_url": "TEAMS_WEBHOOK_URL",
        "jira_enabled": "JIRA_ENABLED",
        "jira_url": "JIRA_URL",
        "jira_email": "JIRA_EMAIL",
        "jira_api_token": "JIRA_API_TOKEN",
        "jira_project_key": "JIRA_PROJECT_KEY",
        "jira_issue_type": "JIRA_ISSUE_TYPE",
    }

    overrides: dict[str, str] = {}
    data = body.model_dump(exclude_none=True)

    for attr, env_key in key_map.items():
        if attr not in data:
            continue
        val = data[attr]
        # Skip masked placeholder — don't overwrite existing secret
        if val == _MASK:
            continue
        overrides[env_key] = val or ""

    if not overrides:
        return {"saved": False, "message": "No changes to save."}

    # Write to .env first, then apply to memory
    _write_env_file(overrides)
    _hot_reload(overrides)

    return {"saved": True, "updated_keys": list(overrides.keys())}


# ── GET /settings/kube-contexts ────────────────────────────────────────────────

@router.get("/kube-contexts")
async def list_kube_contexts():
    """Return available kubectl contexts from kubeconfig."""
    try:
        out = subprocess.check_output(
            ["kubectl", "config", "get-contexts", "-o", "name"],
            text=True, stderr=subprocess.DEVNULL
        ).strip()
        current = subprocess.check_output(
            ["kubectl", "config", "current-context"],
            text=True, stderr=subprocess.DEVNULL
        ).strip()
        contexts = [c for c in out.splitlines() if c]
        return {"contexts": contexts, "current": current}
    except Exception as e:
        return {"contexts": [], "current": "", "error": str(e)}


# ── GET /settings/ollama-models ────────────────────────────────────────────────

@router.get("/ollama-models")
async def list_ollama_models():
    """Return locally available Ollama models."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get("http://localhost:11434/api/tags")
            data = r.json()
            models = [m["name"] for m in data.get("models", [])]
            return {"models": models}
    except Exception as e:
        return {"models": [], "error": str(e)}


# ── POST /settings/test/jira ───────────────────────────────────────────────────

class TestJiraBody(BaseModel):
    jira_url: Optional[str] = None
    jira_email: Optional[str] = None
    jira_api_token: Optional[str] = None


@router.post("/test/jira")
async def test_jira(body: TestJiraBody):
    """Test Jira API connectivity with provided (or current) credentials."""
    url = body.jira_url or settings.jira_url
    email = body.jira_email or settings.jira_email
    token = body.jira_api_token if body.jira_api_token and body.jira_api_token != _MASK \
        else settings.jira_api_token

    if not all([url, email, token]):
        return {"ok": False, "message": "Missing Jira URL, email or API token."}

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"{url.rstrip('/')}/rest/api/3/myself",
                auth=(email, token),
                headers={"Accept": "application/json"},
            )
        if r.status_code == 200:
            data = r.json()
            return {"ok": True, "message": f"Connected as {data.get('displayName', email)}"}
        elif r.status_code == 401:
            return {"ok": False, "message": "Authentication failed — check email and API token."}
        else:
            return {"ok": False, "message": f"Jira returned HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "message": f"Connection error: {e}"}


# ── POST /settings/test/teams ──────────────────────────────────────────────────

class TestTeamsBody(BaseModel):
    teams_webhook_url: Optional[str] = None


@router.post("/test/teams")
async def test_teams(body: TestTeamsBody):
    """Send a test message to Teams via the configured webhook."""
    url = body.teams_webhook_url \
        if body.teams_webhook_url and body.teams_webhook_url != _MASK \
        else settings.teams_webhook_url

    if not url:
        return {"ok": False, "message": "No Teams webhook URL configured."}

    payload = {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "summary": "KIRA Settings Test",
        "themeColor": "7c3aed",
        "sections": [{
            "activityTitle": "✅ KIRA Integration Test",
            "activityText": "Teams integration is working correctly.",
        }]
    }
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.post(url, json=payload)
        if r.status_code in (200, 202):
            return {"ok": True, "message": "Test message sent to Teams successfully."}
        else:
            return {"ok": False, "message": f"Teams returned HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "message": f"Connection error: {e}"}
