"""Background memory monitor — periodically checks pod memory usage against
limits and fires a KIRA incident when any pod exceeds the threshold."""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from typing import Callable, Awaitable

from backend.config import settings

logger = logging.getLogger(__name__)

# System namespaces to skip
_SKIP_NS = {"kube-system", "kube-public", "kube-node-lease"}

# Track which (namespace, deployment) pairs already have an active alert so we
# don't spam duplicate incidents.
_alerted: set[tuple[str, str]] = set()


def _kubectl_base() -> list[str]:
    cmd = ["kubectl"]
    if settings.kube_context:
        cmd += ["--context", settings.kube_context]
    return cmd


def _parse_mi(value: str) -> float:
    """Convert a K8s quantity string to MiB (float)."""
    value = value.strip()
    if m := re.match(r"^(\d+(?:\.\d+)?)Gi$", value):
        return float(m.group(1)) * 1024
    if m := re.match(r"^(\d+(?:\.\d+)?)Mi$", value):
        return float(m.group(1))
    if m := re.match(r"^(\d+(?:\.\d+)?)Ki$", value):
        return float(m.group(1)) / 1024
    if m := re.match(r"^(\d+)$", value):
        return float(m.group(1)) / (1024 * 1024)
    return 0.0


def _get_pod_metrics() -> dict[str, dict[str, float]]:
    """Run kubectl top pods --all-namespaces and return {pod_name: {ns, cpu_m, mem_mi}}."""
    result = subprocess.run(
        _kubectl_base() + ["top", "pods", "--all-namespaces", "--no-headers"],
        capture_output=True, text=True, timeout=15,
    )
    metrics: dict[str, dict[str, float]] = {}
    if result.returncode != 0:
        return metrics
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        ns, pod, cpu_raw, mem_raw = parts[0], parts[1], parts[2], parts[3]
        if ns in _SKIP_NS:
            continue
        metrics[pod] = {
            "namespace": ns,
            "mem_mi": _parse_mi(mem_raw),
        }
    return metrics


def _get_pod_limits() -> dict[str, dict]:
    """Return {pod_name: {namespace, deployment, mem_limit_mi}} for all pods."""
    from backend.integrations.k8s_client import _load_kube_config  # noqa: PLC0415
    from kubernetes import client  # noqa: PLC0415
    _load_kube_config()

    namespaces_cfg = settings.memory_monitor_namespaces.strip()
    target_ns: list[str] = [n.strip() for n in namespaces_cfg.split(",") if n.strip()]

    v1 = client.CoreV1Api()
    if target_ns:
        all_pods = []
        for ns in target_ns:
            all_pods.extend(v1.list_namespaced_pod(namespace=ns).items)
    else:
        all_pods = [
            p for p in v1.list_pod_for_all_namespaces().items
            if p.metadata.namespace not in _SKIP_NS
        ]

    limits: dict[str, dict] = {}
    for pod in all_pods:
        ns = pod.metadata.namespace
        pod_name = pod.metadata.name
        # Derive deployment name from owner references
        deployment = pod_name  # fallback
        for ref in (pod.metadata.owner_references or []):
            if ref.kind == "ReplicaSet":
                # Strip the ReplicaSet hash suffix to get deployment name
                deployment = re.sub(r"-[a-z0-9]+-[a-z0-9]+$", "", ref.name)
                break

        mem_limit_mi = 0.0
        for c in pod.spec.containers:
            if c.resources and c.resources.limits:
                raw = c.resources.limits.get("memory", "")
                if raw:
                    mem_limit_mi += _parse_mi(raw)

        if mem_limit_mi > 0:
            limits[pod_name] = {
                "namespace": ns,
                "deployment": deployment,
                "mem_limit_mi": mem_limit_mi,
            }
    return limits


async def _check_once(
    create_incident: Callable[[str, str, str], Awaitable[None]],
) -> None:
    """Single memory check pass — fires incidents for pods over threshold."""
    loop = asyncio.get_event_loop()

    try:
        metrics = await loop.run_in_executor(None, _get_pod_metrics)
        limits = await loop.run_in_executor(None, _get_pod_limits)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Memory monitor: data collection failed: %s", exc)
        return

    threshold = settings.memory_monitor_threshold_pct

    for pod_name, usage in metrics.items():
        pod_limits = limits.get(pod_name)
        if not pod_limits:
            continue

        ns = pod_limits["namespace"]
        deployment = pod_limits["deployment"]
        limit_mi = pod_limits["mem_limit_mi"]
        used_mi = usage["mem_mi"]

        if limit_mi <= 0:
            continue

        pct = int(used_mi * 100 / limit_mi)

        if pct >= threshold:
            key = (ns, deployment)
            if key in _alerted:
                logger.debug(
                    "Memory monitor: %s/%s already alerted (%d%%) — skipping",
                    ns, deployment, pct,
                )
                continue

            logger.warning(
                "Memory threshold breached: %s/%s  %.1fMi/%.1fMi (%d%%)",
                ns, deployment, used_mi, limit_mi, pct,
            )
            _alerted.add(key)

            message = (
                f"CRITICAL: {deployment} memory usage at {used_mi:.0f}Mi "
                f"({pct}%%) — limit {limit_mi:.0f}Mi. "
                f"OOM kill risk. Patch memory limit."
            )
            await create_incident(deployment, ns, message)
        elif (ns, deployment) in _alerted and pct < threshold - 10:
            # Clear alert once usage drops well below threshold (after remediation)
            _alerted.discard((ns, deployment))
            logger.info(
                "Memory monitor: %s/%s recovered to %d%% — alert cleared",
                ns, deployment, pct,
            )


async def memory_monitor_loop(
    create_incident: Callable[[str, str, str], Awaitable[None]],
) -> None:
    """Long-running background task. Call once at app startup."""
    interval = settings.memory_monitor_interval_s
    logger.info(
        "Memory monitor started — interval=%ds  threshold=%d%%",
        interval,
        settings.memory_monitor_threshold_pct,
    )
    # Give the app a moment to finish starting
    await asyncio.sleep(15)

    while True:
        await _check_once(create_incident)
        await asyncio.sleep(interval)
