"""Playbook: scale a deployment to a target replica count."""
from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)

DEFAULT_REPLICAS = 3


def execute(
    deployment: str, namespace: str, replicas: int = DEFAULT_REPLICAS
) -> tuple[bool, str]:
    from backend.config import settings  # noqa: PLC0415
    from backend.integrations.k8s_client import resolve_workload_kind  # noqa: PLC0415
    kind = resolve_workload_kind(deployment, namespace)
    cmd = ["kubectl"]
    if settings.kube_context:
        cmd += ["--context", settings.kube_context]
    cmd += ["scale", f"{kind}/{deployment}", f"--replicas={replicas}", "-n", namespace]
    logger.info("Executing scale: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    success = result.returncode == 0
    output = result.stdout.strip() if success else result.stderr.strip()
    return success, output or "No output."


def command_preview(
    deployment: str, namespace: str, replicas: int = DEFAULT_REPLICAS
) -> str:
    from backend.integrations.k8s_client import resolve_workload_kind  # noqa: PLC0415
    kind = resolve_workload_kind(deployment, namespace)
    return (
        f"kubectl scale {kind}/{deployment} "
        f"--replicas={replicas} -n {namespace}"
    )
