"""Kubernetes cluster data gathering — wraps the kubernetes Python SDK and
subprocess kubectl calls."""
from __future__ import annotations

import ast
import logging
import os
import subprocess
import tempfile
from typing import Optional

import yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from backend.config import settings

logger = logging.getLogger(__name__)

_RUNNING_IN_DOCKER = os.path.exists("/.dockerenv")
_patched_kubeconfig_path: Optional[str] = None


def _clean_log_output(raw: "str | bytes") -> str:
    """Normalize Kubernetes log output to clean UTF-8 text.

    The Kubernetes Python SDK sometimes returns str(bytes_obj) — i.e. a string
    whose content looks like  b'[INFO]...\\n...'  with escaped characters.
    ast.literal_eval converts that back to real bytes so we can decode properly.
    """
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    if isinstance(raw, str) and len(raw) > 3 and raw[:2] in ("b'", 'b"'):
        try:
            decoded = ast.literal_eval(raw)
            if isinstance(decoded, bytes):
                return decoded.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    return raw if isinstance(raw, str) else str(raw)


def _ensure_patched_kubeconfig() -> None:
    """Write a host.docker.internal-patched kubeconfig and point KUBECONFIG at it.

    Called once inside Docker so that all kubectl subprocess invocations (in
    this module and in every playbook) automatically use the correct API server
    address without any changes to the on-disk kubeconfig.
    """
    global _patched_kubeconfig_path
    if _patched_kubeconfig_path and os.path.exists(_patched_kubeconfig_path):
        return  # already done

    kubeconfig_file = settings.kubeconfig_path or os.path.expanduser("~/.kube/config")
    try:
        with open(kubeconfig_file) as fh:
            kube_data = yaml.safe_load(fh)

        for cluster_entry in kube_data.get("clusters", []):
            cluster = cluster_entry.get("cluster", {})
            server: str = cluster.get("server", "")
            patched = (
                server
                .replace("https://localhost:", "https://host.docker.internal:")
                .replace("https://127.0.0.1:", "https://host.docker.internal:")
            )
            if patched != server:
                cluster["server"] = patched
                cluster["insecure-skip-tls-verify"] = True
                cluster.pop("certificate-authority", None)
                cluster.pop("certificate-authority-data", None)

        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, prefix="kira_kube_"
        )
        yaml.dump(kube_data, tmp)
        tmp.close()
        _patched_kubeconfig_path = tmp.name
        os.environ["KUBECONFIG"] = _patched_kubeconfig_path
        logger.info("Docker kubeconfig patched → %s", _patched_kubeconfig_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not patch kubeconfig for Docker: %s", exc)


def _load_kube_config() -> None:
    try:
        if settings.kubeconfig_path:
            config.load_kube_config(
                config_file=settings.kubeconfig_path,
                context=settings.kube_context or None,
            )
        else:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config(context=settings.kube_context or None)

        # When running inside Docker, localhost/127.0.0.1 in the kubeconfig
        # refers to the container itself, not the Mac host. Rewrite the
        # cluster server URL to use host.docker.internal instead.
        if _RUNNING_IN_DOCKER:
            # Fix Python SDK config
            cfg = client.Configuration.get_default_copy()
            if cfg.host:
                original = cfg.host
                patched = (
                    cfg.host
                    .replace("https://localhost:", "https://host.docker.internal:")
                    .replace("https://127.0.0.1:", "https://host.docker.internal:")
                )
                if patched != original:
                    cfg.host = patched
                    cfg.verify_ssl = False
                    client.Configuration.set_default(cfg)
                    logger.debug("Rewrote K8s API host: %s → %s", original, patched)

            # Fix kubectl subprocess calls (playbooks + resource usage + rollout history)
            _ensure_patched_kubeconfig()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load kube config: %s", exc)


# Do NOT call _load_kube_config() at module level — the kubeconfig may be
# updated after the process starts (e.g. host.docker.internal patch).
# Each public function calls it so it always reads the current file.


def _kubectl(*args: str) -> list[str]:
    """Build a kubectl command list, injecting --context when configured."""
    cmd = ["kubectl"]
    if settings.kube_context:
        cmd += ["--context", settings.kube_context]
    cmd += list(args)
    return cmd


def resolve_workload_kind(name: str, namespace: str) -> str:
    """Return 'deployment' or 'statefulset' for *name* in *namespace*.

    Tries Deployment first (more common), then StatefulSet.  Returns
    'deployment' as fallback when neither is found so callers never crash.
    """
    _load_kube_config()
    apps_v1 = client.AppsV1Api()
    try:
        apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
        return "deployment"
    except ApiException:
        pass
    try:
        apps_v1.read_namespaced_stateful_set(name=name, namespace=namespace)
        return "statefulset"
    except ApiException:
        pass
    return "deployment"


def _read_workload(name: str, namespace: str):
    """Return the workload object (Deployment or StatefulSet) and its kind."""
    _load_kube_config()
    apps_v1 = client.AppsV1Api()
    try:
        return apps_v1.read_namespaced_deployment(name=name, namespace=namespace), "deployment"
    except ApiException:
        pass
    try:
        return apps_v1.read_namespaced_stateful_set(name=name, namespace=namespace), "statefulset"
    except ApiException:
        pass
    raise ApiException(status=404, reason=f"No deployment or statefulset '{name}' in {namespace}")


def get_pod_status(namespace: str, deployment: str = "") -> str:
    """Return pod status lines.  When *deployment* is provided, only pods
    owned by the deployment's ACTIVE ReplicaSets are shown — this filters
    out stale pods from old rollouts that would mislead the decision engine."""
    _load_kube_config()
    try:
        v1 = client.CoreV1Api()

        if deployment:
            # Find pods belonging to the workload via label selector,
            # then filter to only those owned by an active RS (replicas > 0).
            try:
                apps_v1 = client.AppsV1Api()
                workload, kind = _read_workload(deployment, namespace)
                match_labels = workload.spec.selector.match_labels or {}
                label_selector = ",".join(f"{k}={v}" for k, v in match_labels.items())

                if kind == "deployment":
                    # Get active RS names (replicas > 0)
                    rs_list = apps_v1.list_namespaced_replica_set(
                        namespace=namespace, label_selector=label_selector
                    )
                    active_rs = {
                        rs.metadata.name
                        for rs in rs_list.items
                        if (rs.spec.replicas or 0) > 0
                    }

                    all_pods = v1.list_namespaced_pod(
                        namespace=namespace, label_selector=label_selector
                    )
                    # Keep only pods owned by an active RS
                    pods_items = []
                    for pod in all_pods.items:
                        for ref in (pod.metadata.owner_references or []):
                            if ref.kind == "ReplicaSet" and ref.name in active_rs:
                                pods_items.append(pod)
                                break
                else:
                    # StatefulSet pods are owned directly by the StatefulSet
                    all_pods = v1.list_namespaced_pod(
                        namespace=namespace, label_selector=label_selector
                    )
                    pods_items = list(all_pods.items)
            except Exception:  # noqa: BLE001
                # Fallback: list all pods in namespace
                pods_items = v1.list_namespaced_pod(namespace=namespace).items
        else:
            pods_items = v1.list_namespaced_pod(namespace=namespace).items

        lines: list[str] = []
        for pod in pods_items:
            container_statuses = pod.status.container_statuses or []
            ready = sum(1 for c in container_statuses if c.ready)
            total = len(pod.spec.containers)
            restarts = sum(c.restart_count for c in container_statuses)

            # Determine the display status — mimic kubectl's STATUS column
            # Priority: container waiting reason > terminated reason > pod phase
            phase = pod.status.phase or "Unknown"
            display_status = phase
            last_terminated_reason = ""
            for cs in container_statuses:
                if cs.state:
                    if cs.state.waiting and cs.state.waiting.reason:
                        display_status = cs.state.waiting.reason  # CrashLoopBackOff, ImagePullBackOff, etc.
                        break
                    if cs.state.terminated and cs.state.terminated.reason:
                        reason = cs.state.terminated.reason
                        # If container terminated with Error and has restarts,
                        # it's effectively CrashLoopBackOff (just caught between restarts)
                        if reason in ("Error", "OOMKilled") and restarts >= 2:
                            display_status = "CrashLoopBackOff" if reason == "Error" else reason
                        else:
                            display_status = reason
                        break
                # Also capture last_state for OOMKilled detection (restarts)
                if cs.last_state and cs.last_state.terminated:
                    lr = cs.last_state.terminated.reason
                    if lr:
                        last_terminated_reason = lr

            # If the container is running but was OOMKilled last time, annotate
            reason_suffix = ""
            if last_terminated_reason and last_terminated_reason != display_status:
                reason_suffix = f"  LastTerminated:{last_terminated_reason}"

            lines.append(
                f"{pod.metadata.name}  {display_status}  {ready}/{total} Ready  Restarts:{restarts}{reason_suffix}"
            )
        return "\n".join(lines) if lines else "No pods found."
    except ApiException as exc:
        return f"Error fetching pod status: {exc.reason}"


def get_pod_logs(pod_name: str, namespace: str, tail: int = 100) -> str:
    _load_kube_config()
    try:
        v1 = client.CoreV1Api()
        logs = v1.read_namespaced_pod_log(
            name=pod_name, namespace=namespace, tail_lines=tail
        )
        return _clean_log_output(logs) or "(no log output)"
    except ApiException as exc:
        return f"Error fetching logs for {pod_name}: {exc.reason}"


def get_logs_for_deployment(deployment: str, namespace: str, tail: int = 80) -> str:
    """Fetch logs from the first available pod that belongs to *deployment*."""
    _load_kube_config()
    try:
        v1 = client.CoreV1Api()
        pods = v1.list_namespaced_pod(
            namespace=namespace, label_selector=f"app={deployment}"
        )
        if not pods.items:
            # Fallback: match by name substring
            all_pods = v1.list_namespaced_pod(namespace=namespace)
            pods.items = [p for p in all_pods.items if deployment in p.metadata.name]
        if not pods.items:
            return f"No pods found for deployment '{deployment}'."
        pod_name = pods.items[0].metadata.name
        return get_pod_logs(pod_name, namespace, tail)
    except ApiException as exc:
        return f"Error fetching deployment logs: {exc.reason}"


def get_resource_usage(namespace: str) -> str:
    """Run kubectl top pods (requires metrics-server addon in minikube)."""
    result = subprocess.run(
        _kubectl("top", "pods", "-n", namespace, "--no-headers"),
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode == 0:
        return result.stdout.strip() or "No resource data available."
    return f"kubectl top unavailable (metrics-server may not be running): {result.stderr.strip()}"


def get_rollout_history(deployment: str, namespace: str) -> str:
    kind = resolve_workload_kind(deployment, namespace)
    result = subprocess.run(
        _kubectl("rollout", "history", f"{kind}/{deployment}", "-n", namespace),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return f"Rollout history unavailable: {result.stderr.strip()}"


def get_deployment_describe(deployment: str, namespace: str) -> str:
    _load_kube_config()
    try:
        workload, kind = _read_workload(deployment, namespace)
        kind_label = "Deployment" if kind == "deployment" else "StatefulSet"
        lines: list[str] = [
            f"{kind_label}: {deployment}",
            f"Desired replicas: {workload.spec.replicas}",
        ]
        for c in workload.spec.template.spec.containers:
            res = c.resources
            limits = (res.limits or {}) if res else {}
            requests_val = (res.requests or {}) if res else {}
            lines.append(
                f"  Container: {c.name}  image={c.image}"
                f"  limits={limits}  requests={requests_val}"
            )
        s = workload.status
        lines.append(
            f"Status: desired={workload.spec.replicas}"
            f"  available={getattr(s, 'available_replicas', None)}"
            f"  ready={s.ready_replicas}"
            f"  updated={getattr(s, 'updated_replicas', None) or s.updated_replicas}"
        )
        return "\n".join(lines)
    except ApiException as exc:
        return f"Error describing workload: {exc.reason}"


def get_deployment_info(deployment: str, namespace: str) -> dict:
    """Return structured deployment data directly from the K8s API.

    Unlike get_deployment_describe() (text for LLMs), this returns a dict
    that the decision engine can consume without any regex/string parsing.
    Works for ANY service regardless of naming or resource configuration.
    """
    _load_kube_config()
    try:
        workload, kind = _read_workload(deployment, namespace)
        s = workload.status
        containers = []
        for c in workload.spec.template.spec.containers:
            res = c.resources
            limits = dict(res.limits) if res and res.limits else {}
            requests = dict(res.requests) if res and res.requests else {}
            containers.append({
                "name": c.name,
                "image": c.image,
                "limits": limits,
                "requests": requests,
            })
        return {
            "deployment": deployment,
            "kind": kind,
            "namespace": namespace,
            "desired": workload.spec.replicas or 1,
            "available": getattr(s, "available_replicas", None) or 0,
            "ready": s.ready_replicas or 0,
            "updated": getattr(s, "updated_replicas", None) or 0,
            "containers": containers,
        }
    except ApiException as exc:
        logger.warning("get_deployment_info failed for %s/%s: %s", namespace, deployment, exc.reason)
        return {}


def get_recent_events(namespace: str, deployment: str = "") -> str:
    """Return recent warning events.  When *deployment* is given, only events
    whose involved object name matches a pod from the deployment's CURRENT
    ReplicaSet are included — this filters out stale events from old rollouts."""
    _load_kube_config()
    try:
        v1 = client.CoreV1Api()

        # Build set of current pod names for the deployment (if provided)
        current_pod_prefixes: set[str] = set()
        if deployment:
            try:
                workload, _ = _read_workload(deployment, namespace)
                match_labels = workload.spec.selector.match_labels or {}
                pods = v1.list_namespaced_pod(
                    namespace=namespace,
                    label_selector=",".join(f"{k}={v}" for k, v in match_labels.items()),
                )
                current_pod_prefixes = {p.metadata.name for p in pods.items}
            except Exception:  # noqa: BLE001
                pass  # fall back to unfiltered

        events = v1.list_namespaced_event(namespace=namespace)
        warnings = [e for e in events.items if e.type == "Warning"]
        warnings.sort(
            key=lambda e: e.last_timestamp or e.event_time or "",  # type: ignore[arg-type]
            reverse=True,
        )

        if current_pod_prefixes:
            warnings = [
                e for e in warnings
                if e.involved_object.name in current_pod_prefixes
                or e.involved_object.name == deployment
            ]

        lines = [
            f"[{e.type}] {e.reason}: {e.message} (obj: {e.involved_object.name})"
            for e in warnings[:10]
        ]
        return "\n".join(lines) if lines else "No warning events found."
    except ApiException as exc:
        return f"Error fetching events: {exc.reason}"


def get_correlated_service_logs(primary_service: str, namespace: str) -> str:
    """Find all other workloads in the namespace that have unhealthy pods
    and return a brief log snippet for each — reveals cascade failures."""
    _load_kube_config()
    try:
        apps_v1 = client.AppsV1Api()
        results: list[str] = []

        # Check Deployments
        deployments = apps_v1.list_namespaced_deployment(namespace=namespace)
        for dep in deployments.items:
            name = dep.metadata.name
            if name == primary_service:
                continue
            desired = dep.spec.replicas or 1
            ready = dep.status.ready_replicas or 0
            if ready < desired:
                logs = get_logs_for_deployment(name, namespace, tail=25)
                results.append(
                    f"--- {name} (ready={ready}/{desired} replicas) ---\n{logs}"
                )

        # Check StatefulSets
        statefulsets = apps_v1.list_namespaced_stateful_set(namespace=namespace)
        for sts in statefulsets.items:
            name = sts.metadata.name
            if name == primary_service:
                continue
            desired = sts.spec.replicas or 1
            ready = sts.status.ready_replicas or 0
            if ready < desired:
                logs = get_logs_for_deployment(name, namespace, tail=25)
                results.append(
                    f"--- {name} (ready={ready}/{desired} replicas) ---\n{logs}"
                )

        return "\n\n".join(results) if results else "No other failing services detected."
    except ApiException as exc:
        return f"Error scanning correlated services: {exc.reason}"


def get_rollout_progress(deployment: str, namespace: str) -> dict:
    """Return structured rollout progress for a workload.

    Returns a dict with: desired, updated, ready, available, kind,
    observed_generation, current_generation, and a boolean 'progressing'
    that indicates whether the rollout is actively making progress
    (updated/ready count is increasing toward desired).
    """
    _load_kube_config()
    result = {"desired": 0, "updated": 0, "ready": 0, "available": 0,
              "kind": "unknown", "progressing": False,
              "observed_generation": 0, "current_generation": 0,
              "error_pods": 0}
    try:
        workload, kind = _read_workload(deployment, namespace)
        result["kind"] = kind
        status = workload.status
        result["desired"] = workload.spec.replicas or 1
        result["updated"] = getattr(status, "updated_replicas", None) or 0
        result["ready"] = status.ready_replicas or 0
        result["available"] = getattr(status, "available_replicas", None) or result["ready"]
        result["observed_generation"] = status.observed_generation or 0
        result["current_generation"] = workload.metadata.generation or 0

        # Count pods in terminal error states vs pods actively starting
        v1 = client.CoreV1Api()
        pods = v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"app={deployment}",
        )
        terminal_errors = 0
        error_reasons = ("imagepullbackoff", "errimagepull",
                         "crashloopbackoff", "createcontainererror",
                         "createcontainerconfigerror")
        for pod in pods.items:
            pod_has_error = False
            # Check regular containers
            for cs in (pod.status.container_statuses or []):
                if cs.state and cs.state.waiting:
                    reason = (cs.state.waiting.reason or "").lower()
                    if reason in error_reasons:
                        pod_has_error = True
                        break
                # Also catch OOMKilled — shows as terminated, not waiting
                if cs.state and cs.state.terminated:
                    reason = (cs.state.terminated.reason or "").lower()
                    if reason == "oomkilled":
                        pod_has_error = True
                        break
            # Check init containers too
            if not pod_has_error:
                for cs in (pod.status.init_container_statuses or []):
                    if cs.state and cs.state.waiting:
                        reason = (cs.state.waiting.reason or "").lower()
                        if reason in error_reasons:
                            pod_has_error = True
                            break
            if pod_has_error:
                terminal_errors += 1
        result["error_pods"] = terminal_errors

        # Rollout is progressing if: spec has been observed, some pods are
        # updated/ready, and not all pods are stuck in error states.
        spec_observed = result["observed_generation"] >= result["current_generation"]
        has_progress = result["updated"] > 0 or result["ready"] > 0
        not_all_stuck = terminal_errors < result["desired"]
        result["progressing"] = spec_observed and has_progress and not_all_stuck

    except ApiException:
        pass
    return result


def is_deployment_healthy(deployment: str, namespace: str) -> bool:
    """True only when the rollout is fully complete — all pods are updated,
    ready, available, and none are in error states (ImagePullBackOff, etc.).
    Works for both Deployments and StatefulSets."""
    _load_kube_config()
    try:
        workload, kind = _read_workload(deployment, namespace)
        desired = workload.spec.replicas or 1
        status = workload.status

        # K8s must have processed the latest spec change
        observed_gen = status.observed_generation or 0
        current_gen = workload.metadata.generation or 0
        if observed_gen < current_gen:
            return False

        updated = getattr(status, "updated_replicas", None) or 0
        ready = status.ready_replicas or 0
        available = getattr(status, "available_replicas", None) or ready
        unavailable = getattr(status, "unavailable_replicas", None) or 0

        # All replicas must be on the latest template, ready, and none unavailable
        if updated < desired or ready < desired or available < desired:
            return False
        if unavailable > 0:
            return False

        # Pod-level: reject if ANY pod is in an error state
        v1 = client.CoreV1Api()
        pods = v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"app={deployment}",
        )
        for pod in pods.items:
            phase = (pod.status.phase or "").lower()
            if phase in ("failed", "unknown"):
                return False
            for cs in (pod.status.container_statuses or []):
                if cs.state and cs.state.waiting:
                    reason = (cs.state.waiting.reason or "").lower()
                    if reason in ("imagepullbackoff", "errimagepull",
                                  "crashloopbackoff", "createcontainererror"):
                        return False

        return True
    except ApiException:
        return False
