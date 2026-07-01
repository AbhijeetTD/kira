"""Playbook: intelligently patch CPU and/or memory resource limits on a
deployment container.

Analyses actual pod resource usage vs configured limits/requests to decide
WHAT to patch (CPU, memory, or both) and by how much.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess

logger = logging.getLogger(__name__)

DEFAULT_CPU_LIMIT = "500m"
DEFAULT_MEMORY_LIMIT = "512Mi"


# ── Unit helpers ─────────────────────────────────────────────────────────────

def _parse_mi(value: str) -> int:
    """Convert a K8s memory string to MiB integer (best-effort)."""
    value = value.strip()
    if m := re.match(r"^(\d+)Mi$", value):
        return int(m.group(1))
    if m := re.match(r"^(\d+)Gi$", value):
        return int(m.group(1)) * 1024
    if m := re.match(r"^(\d+)$", value):
        return int(m) // (1024 * 1024)
    return 512  # fallback


def _format_mi(mi: int) -> str:
    """Format MiB as a K8s string (use Gi when appropriate)."""
    if mi >= 1024 and mi % 1024 == 0:
        return f"{mi // 1024}Gi"
    return f"{mi}Mi"


def _parse_millicores(value: str) -> int:
    """Convert a K8s CPU string to millicores."""
    value = value.strip()
    if m := re.match(r"^(\d+)m$", value):
        return int(m.group(1))
    if m := re.match(r"^(\d+)$", value):
        return int(m.group(1)) * 1000
    return 500  # fallback


def _format_millicores(mc: int) -> str:
    """Format millicores as a K8s string."""
    if mc >= 1000 and mc % 1000 == 0:
        return str(mc // 1000)
    return f"{mc}m"


# ── Introspection ────────────────────────────────────────────────────────────

def _read_current_limits(deployment: str, namespace: str, container: str) -> dict:
    """Return current limits AND requests from the live deployment spec."""
    info = {
        "cpu_limit": DEFAULT_CPU_LIMIT,
        "memory_limit": DEFAULT_MEMORY_LIMIT,
        "cpu_request": "100m",
        "memory_request": "128Mi",
    }
    try:
        from backend.integrations.k8s_client import _load_kube_config  # noqa: PLC0415
        from kubernetes import client  # noqa: PLC0415
        _load_kube_config()
        dep = client.AppsV1Api().read_namespaced_deployment(
            name=deployment, namespace=namespace
        )
        for c in dep.spec.template.spec.containers:
            if c.name == container and c.resources:
                limits = c.resources.limits or {}
                requests = c.resources.requests or {}
                info["cpu_limit"] = limits.get("cpu", DEFAULT_CPU_LIMIT)
                info["memory_limit"] = limits.get("memory", DEFAULT_MEMORY_LIMIT)
                info["cpu_request"] = requests.get("cpu", "100m")
                info["memory_request"] = requests.get("memory", "128Mi")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read current limits for %s: %s", deployment, exc)
    return info


def _get_pod_usage(deployment: str, namespace: str) -> dict:
    """Return average CPU (millicores) and memory (MiB) usage across pods."""
    from backend.config import settings  # noqa: PLC0415
    cmd = ["kubectl"]
    if settings.kube_context:
        cmd += ["--context", settings.kube_context]
    cmd += ["top", "pods", "-n", namespace, "-l", f"app={deployment}", "--no-headers"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        return {"cpu_mc": 0, "mem_mi": 0}

    total_cpu, total_mem, count = 0, 0, 0
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 3:
            cpu_str = parts[1]  # e.g. "1m" or "250m"
            mem_str = parts[2]  # e.g. "2Mi" or "128Mi"
            total_cpu += _parse_millicores(cpu_str)
            total_mem += _parse_mi(mem_str)
            count += 1
    if count == 0:
        return {"cpu_mc": 0, "mem_mi": 0}
    return {"cpu_mc": total_cpu // count, "mem_mi": total_mem // count}


def _decide_patch(current: dict, usage: dict) -> dict:
    """Decide what to patch based on usage vs limits/requests.

    Returns a dict with keys: cpu_limit, memory_limit, cpu_request,
    memory_request — only changed values differ from *current*.
    Also includes 'changes' list describing what changed and why.
    """
    changes: list[str] = []
    new = dict(current)  # start with current values

    cur_cpu_limit_mc = _parse_millicores(current["cpu_limit"])
    cur_cpu_req_mc = _parse_millicores(current["cpu_request"])
    cur_mem_limit_mi = _parse_mi(current["memory_limit"])
    cur_mem_req_mi = _parse_mi(current["memory_request"])
    usage_cpu_mc = usage.get("cpu_mc", 0)
    usage_mem_mi = usage.get("mem_mi", 0)

    # ── CPU analysis ─────────────────────────────────────────────────────
    # Flag: CPU limit is dangerously low (< 50m) or usage is above 70% of limit
    cpu_constrained = False
    if cur_cpu_limit_mc < 50:
        cpu_constrained = True
        reason = f"CPU limit {current['cpu_limit']} is dangerously low"
    elif usage_cpu_mc > 0 and usage_cpu_mc > cur_cpu_limit_mc * 0.7:
        cpu_constrained = True
        reason = f"CPU usage {usage_cpu_mc}m is >{70}% of limit {current['cpu_limit']}"
    elif cur_cpu_req_mc > cur_cpu_limit_mc:
        cpu_constrained = True
        reason = f"CPU request {current['cpu_request']} exceeds limit {current['cpu_limit']}"

    if cpu_constrained:
        # Set CPU limit to at least 2x current or 500m, whichever is larger
        new_cpu_mc = max(cur_cpu_limit_mc * 2, 500)
        # Also ensure request <= limit
        new_cpu_req_mc = max(cur_cpu_req_mc, min(new_cpu_mc // 2, 250))
        new["cpu_limit"] = _format_millicores(new_cpu_mc)
        new["cpu_request"] = _format_millicores(new_cpu_req_mc)
        changes.append(f"CPU limit: {current['cpu_limit']} → {new['cpu_limit']} ({reason})")
        if new["cpu_request"] != current["cpu_request"]:
            changes.append(f"CPU request: {current['cpu_request']} → {new['cpu_request']}")

    # ── Memory analysis ──────────────────────────────────────────────────
    mem_constrained = False
    if cur_mem_limit_mi < 64:
        mem_constrained = True
        reason = f"Memory limit {current['memory_limit']} is dangerously low"
    elif usage_mem_mi > 0 and usage_mem_mi > cur_mem_limit_mi * 0.7:
        mem_constrained = True
        reason = f"Memory usage {usage_mem_mi}Mi is >{70}% of limit {current['memory_limit']}"
    elif cur_mem_req_mi > cur_mem_limit_mi:
        mem_constrained = True
        reason = f"Memory request {current['memory_request']} exceeds limit {current['memory_limit']}"

    if mem_constrained:
        new_mem_mi = max(cur_mem_limit_mi * 2, 256)
        new_mem_req_mi = max(cur_mem_req_mi, min(new_mem_mi // 2, 256))
        new["memory_limit"] = _format_mi(new_mem_mi)
        new["memory_request"] = _format_mi(new_mem_req_mi)
        changes.append(f"Memory limit: {current['memory_limit']} → {new['memory_limit']} ({reason})")
        if new["memory_request"] != current["memory_request"]:
            changes.append(f"Memory request: {current['memory_request']} → {new['memory_request']}")

    # ── Fallback: if nothing detected, double memory ─────────────────────
    if not changes:
        new_mem_mi = max(cur_mem_limit_mi * 2, 256)
        new["memory_limit"] = _format_mi(new_mem_mi)
        changes.append(
            f"Memory limit: {current['memory_limit']} → {new['memory_limit']} "
            f"(no specific bottleneck detected — increasing memory as precaution)"
        )

    new["changes"] = changes
    return new


# ── Execute ──────────────────────────────────────────────────────────────────

def execute(
    deployment: str,
    namespace: str,
    container: str,
    cpu_limit: str = "auto",
    memory_limit: str = "auto",
) -> tuple[bool, str]:
    """Patch resource limits on *container* inside *deployment*.

    With ``"auto"`` (default), analyses actual usage vs limits and patches
    whichever resource is constrained (CPU, memory, or both).
    """
    current = _read_current_limits(deployment, namespace, container)

    if cpu_limit == "auto" and memory_limit == "auto":
        usage = _get_pod_usage(deployment, namespace)
        decision = _decide_patch(current, usage)
        resolved_cpu = decision["cpu_limit"]
        resolved_mem = decision["memory_limit"]
        resolved_cpu_req = decision["cpu_request"]
        resolved_mem_req = decision["memory_request"]
        change_summary = " | ".join(decision["changes"])
        logger.info("Patch decision for %s/%s: %s", namespace, deployment, change_summary)
    else:
        resolved_cpu = current["cpu_limit"] if cpu_limit == "auto" else cpu_limit
        resolved_mem = current["memory_limit"] if memory_limit == "auto" else memory_limit
        resolved_cpu_req = current["cpu_request"]
        resolved_mem_req = current["memory_request"]
        change_summary = f"Manual override: CPU={resolved_cpu}, Memory={resolved_mem}"

    patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": container,
                            "resources": {
                                "limits": {
                                    "cpu": resolved_cpu,
                                    "memory": resolved_mem,
                                },
                                "requests": {
                                    "cpu": resolved_cpu_req,
                                    "memory": resolved_mem_req,
                                },
                            },
                        }
                    ]
                }
            }
        }
    }
    from backend.config import settings  # noqa: PLC0415
    from backend.integrations.k8s_client import resolve_workload_kind  # noqa: PLC0415
    kind = resolve_workload_kind(deployment, namespace)
    cmd = ["kubectl"]
    if settings.kube_context:
        cmd += ["--context", settings.kube_context]
    cmd += [
        "patch", f"{kind}/{deployment}",
        "-n", namespace,
        "--type=strategic",
        "-p", json.dumps(patch),
    ]
    logger.info("Executing patch: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    success = result.returncode == 0
    output = result.stdout.strip() if success else result.stderr.strip()
    if success:
        output = f"{output}\n{change_summary}"
    return success, output or "No output."


def command_preview(deployment: str, namespace: str, container: str) -> str:
    from backend.integrations.k8s_client import resolve_workload_kind  # noqa: PLC0415
    kind = resolve_workload_kind(deployment, namespace)
    current = _read_current_limits(deployment, namespace, container)
    usage = _get_pod_usage(deployment, namespace)
    decision = _decide_patch(current, usage)
    what = []
    if decision["cpu_limit"] != current["cpu_limit"]:
        what.append(f"CPU {current['cpu_limit']}→{decision['cpu_limit']}")
    if decision["memory_limit"] != current["memory_limit"]:
        what.append(f"Mem {current['memory_limit']}→{decision['memory_limit']}")
    detail = ", ".join(what) if what else "auto-adjust resources"
    return (
        f"kubectl patch {kind}/{deployment} -n {namespace} "
        f"({detail} on container '{container}')"
    )

