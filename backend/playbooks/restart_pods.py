"""Playbook: restart all pods in a deployment via rollout restart."""
from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)


def execute(deployment: str, namespace: str) -> tuple[bool, str]:
    from backend.config import settings  # noqa: PLC0415
    from backend.integrations.k8s_client import resolve_workload_kind  # noqa: PLC0415
    kind = resolve_workload_kind(deployment, namespace)
    cmd = ["kubectl"]
    if settings.kube_context:
        cmd += ["--context", settings.kube_context]
    cmd += ["rollout", "restart", f"{kind}/{deployment}", "-n", namespace]
    logger.info("Executing restart: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    success = result.returncode == 0
    output = result.stdout.strip() if success else result.stderr.strip()
    return success, output or "No output."


def command_preview(deployment: str, namespace: str) -> str:
    from backend.integrations.k8s_client import resolve_workload_kind  # noqa: PLC0415
    kind = resolve_workload_kind(deployment, namespace)
    return f"kubectl rollout restart {kind}/{deployment} -n {namespace}"
