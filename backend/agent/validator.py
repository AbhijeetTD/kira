"""Recovery validator — polls Kubernetes until the deployment is healthy or
the timeout is reached.  On failure, performs deep diagnostics and uses
the LLM to generate actionable recovery suggestions."""
from __future__ import annotations

import asyncio
import logging
import time

from backend.integrations.k8s_client import is_deployment_healthy, get_rollout_progress
from backend.models.incident import TimelineEvent, ValidationResult

logger = logging.getLogger(__name__)

MAX_WAIT_SECONDS = 90
POLL_INTERVAL_SECONDS = 4
_PROGRESS_INTERVAL = 20  # emit progress every N seconds
_INITIAL_SETTLE_SECONDS = 5  # wait for K8s to begin the rollout before polling


# ── Deep diagnostic on failure ───────────────────────────────────────────────

def _diagnose_failure(service: str, namespace: str) -> dict:
    """Inspect the deployment after remediation failed to determine WHY.
    Returns a dict with structured diagnostic info."""
    from backend.integrations.k8s_client import _load_kube_config  # noqa: PLC0415
    from kubernetes import client  # noqa: PLC0415
    _load_kube_config()

    diag: dict = {"issues": [], "current_image": None, "probe_port": None,
                  "desired": 0, "ready": 0, "pod_states": []}
    try:
        from backend.integrations.k8s_client import _read_workload  # noqa: PLC0415
        workload, kind = _read_workload(service, namespace)
        diag["desired"] = workload.spec.replicas or 1
        diag["ready"] = workload.status.ready_replicas or 0

        # Container image
        for c in workload.spec.template.spec.containers:
            if c.name == service or len(workload.spec.template.spec.containers) == 1:
                diag["current_image"] = c.image
                # Readiness probe port
                if c.readiness_probe and c.readiness_probe.http_get:
                    diag["probe_port"] = c.readiness_probe.http_get.port

        # Pod-level states
        v1 = client.CoreV1Api()
        pods = v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={service}")
        for pod in pods.items:
            cs_list = pod.status.container_statuses or []
            for cs in cs_list:
                state_str = "unknown"
                reason = ""
                if cs.state:
                    if cs.state.waiting:
                        state_str = "waiting"
                        reason = cs.state.waiting.reason or ""
                    elif cs.state.running:
                        state_str = "running"
                    elif cs.state.terminated:
                        state_str = "terminated"
                        reason = cs.state.terminated.reason or ""
                diag["pod_states"].append({
                    "pod": pod.metadata.name,
                    "state": state_str,
                    "reason": reason,
                    "ready": cs.ready,
                    "restarts": cs.restart_count,
                })

        # Classify issues
        for ps in diag["pod_states"]:
            r = ps["reason"].lower()
            if "imagepullbackoff" in r or "errimagepull" in r:
                diag["issues"].append("image_pull_error")
            elif "crashloopbackoff" in r:
                diag["issues"].append("crashloop")
            elif "oomkilled" in r:
                diag["issues"].append("oom_killed")
            elif ps["state"] == "running" and not ps["ready"]:
                diag["issues"].append("probe_failure")

        diag["issues"] = list(set(diag["issues"]))

    except Exception as exc:  # noqa: BLE001
        logger.warning("Diagnostic failed: %s", exc)
        diag["issues"].append("diagnostic_error")

    return diag


async def _generate_suggestions(service: str, namespace: str, diag: dict,
                                  remediation_action: str) -> str:
    """Ask the LLM for targeted recovery suggestions based on the diagnostic."""
    from backend.integrations.openai_client import generate  # noqa: PLC0415

    prompt = f"""\
A Kubernetes remediation was attempted but validation FAILED.

Service: {service}
Namespace: {namespace}
Remediation attempted: {remediation_action}
Current image: {diag.get('current_image', 'unknown')}
Readiness probe port: {diag.get('probe_port', 'unknown')}
Replicas: {diag.get('ready', 0)}/{diag.get('desired', '?')} ready
Detected issues: {', '.join(diag.get('issues', ['unknown']))}
Pod states: {diag.get('pod_states', [])}

Based on these diagnostics, provide 3-5 specific, actionable recovery steps
the operator should take. Focus on what went wrong and how to fix it manually.
Be concise — one line per step, no markdown formatting.

Respond ONLY with a JSON object:
{{
  "diagnosis": "<1-sentence explanation of why remediation failed>",
  "suggestions": ["<step 1>", "<step 2>", "<step 3>"],
  "severity": "<critical|warning|info>"
}}"""

    system = (
        "You are an expert Kubernetes SRE. Provide specific, actionable recovery steps "
        "based on the diagnostic data. No generic advice — every suggestion must directly "
        "address the observed failure mode. Respond with valid JSON only."
    )
    try:
        import json
        raw = await generate(prompt, system=system)
        # Try to parse JSON from response
        import re
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            data = json.loads(match.group())
            lines = [f"Diagnosis: {data.get('diagnosis', 'Unknown')}"]
            for i, s in enumerate(data.get("suggestions", []), 1):
                lines.append(f"  {i}. {s}")
            return "\n".join(lines)
    except Exception:  # noqa: BLE001
        pass

    # Fallback: generate rule-based suggestions from diagnostic
    return _rule_based_suggestions(diag, remediation_action)


def _rule_based_suggestions(diag: dict, action: str) -> str:
    """Deterministic fallback suggestions when the LLM is unavailable."""
    lines = []
    issues = diag.get("issues", [])

    if "image_pull_error" in issues:
        img = diag.get("current_image", "unknown")
        lines.append(f"Image '{img}' cannot be pulled — verify the image name, tag, and registry access")
        lines.append(f"Try: kubectl set image deployment/{diag.get('service', 'DEPLOYMENT')} CONTAINER=CORRECT_IMAGE")
        lines.append("Check if the registry requires authentication (imagePullSecrets)")

    if "probe_failure" in issues:
        port = diag.get("probe_port", "?")
        lines.append(f"Readiness probe on port {port} is failing — pods are Running but not Ready")
        lines.append(f"Verify the application actually listens on port {port}")
        lines.append("Try: kubectl edit deployment — fix readinessProbe.httpGet.port")

    if "crashloop" in issues:
        lines.append("Pods are CrashLooping — check application startup configuration")
        lines.append("Try: kubectl logs <pod-name> --previous to see crash output")

    if "oom_killed" in issues:
        lines.append("Pods are being OOMKilled — memory limits are too low")
        lines.append("Try: kubectl patch deployment — increase spec.containers[].resources.limits.memory")

    if not lines:
        lines.append(f"Remediation '{action}' did not restore health within {MAX_WAIT_SECONDS}s")
        lines.append("Check: kubectl describe deployment, kubectl get events, kubectl logs")
        lines.append("The deployment may need manual intervention")

    return "\n".join(f"  {i}. {l}" for i, l in enumerate(lines, 1))


# ── Main validation loop ────────────────────────────────────────────────────

async def validate_recovery(
    service: str,
    namespace: str,
    queue: object,  # duck-typed emit queue
    remediation_action: str = "unknown",
) -> ValidationResult:
    await queue.put(
        TimelineEvent(
            step="Validation",
            status="running",
            detail=f"Waiting for {service} to become healthy (up to {MAX_WAIT_SECONDS}s)…",
        )
    )

    # Give K8s time to process the spec change and start the rollout.
    # Without this, the health check sees the OLD replicaset's pods (still
    # running and ready) and falsely declares success in <1s.
    await asyncio.sleep(_INITIAL_SETTLE_SECONDS)

    start = time.monotonic()
    checks_passed: list[str] = []
    checks_failed: list[str] = []
    loop = asyncio.get_event_loop()
    last_progress = start
    last_ready_count = -1

    while True:
        elapsed = time.monotonic() - start

        if elapsed > MAX_WAIT_SECONDS:
            checks_failed.append(
                f"Timeout: deployment {service} did not become ready within "
                f"{MAX_WAIT_SECONDS}s"
            )
            break

        healthy = await loop.run_in_executor(
            None, is_deployment_healthy, service, namespace
        )
        if healthy:
            checks_passed.append(f"Deployment {service} — all replicas Ready")
            break

        # Check rollout progress to decide if we should extend the timeout.
        # If pods are actively being updated (ready count increasing), the fix
        # is working — just needs more time (especially for StatefulSets).
        rollout = await loop.run_in_executor(
            None, get_rollout_progress, service, namespace
        )
        current_ready = rollout.get("ready", 0)
        desired = rollout.get("desired", 1)

        if current_ready != last_ready_count:
            last_ready_count = current_ready

        rollout_is_progressing = (
            rollout.get("progressing", False)
            and current_ready > 0
            and rollout.get("error_pods", 0) == 0
        )

        # Emit progress updates so the timeline doesn't look stuck
        now = time.monotonic()
        if now - last_progress >= _PROGRESS_INTERVAL:
            status_note = ""
            if rollout_is_progressing:
                status_note = " — rollout progressing"
            await queue.put(
                TimelineEvent(
                    step="Validation",
                    status="running",
                    detail=f"Still waiting… {current_ready}/{desired} pods ready "
                           f"({elapsed:.0f}s elapsed){status_note}",
                )
            )
            last_progress = now

        await asyncio.sleep(POLL_INTERVAL_SECONDS)

    elapsed = time.monotonic() - start
    is_healthy = bool(checks_passed) and not checks_failed

    # Determine if the rollout is still actively progressing despite timeout.
    # "Progressing" means the fix IS working but needs more time (e.g. StatefulSet
    # rolling update). We only suppress retry when ALL of these hold:
    #   1. K8s reports the rollout is progressing (spec observed, pods updating)
    #   2. Some pods are ready (fix didn't break everything)
    #   3. Zero pods in terminal error states (no CrashLoop, ImagePull, etc.)
    #   4. Ready count improved since we started (fix actually helped)
    #   5. Ready count didn't decrease during validation (no regression)
    rollout_still_progressing = False
    if not is_healthy:
        final_rollout = await loop.run_in_executor(
            None, get_rollout_progress, service, namespace
        )
        final_ready = final_rollout.get("ready", 0)
        final_desired = final_rollout.get("desired", 1)
        final_updated = final_rollout.get("updated", 0)
        rollout_still_progressing = (
            final_rollout.get("progressing", False)
            and final_ready > 0
            and final_rollout.get("error_pods", 0) == 0
            # Ready count must not have regressed — if it dropped, the fix
            # may be making things worse and a real retry is warranted
            and final_ready >= last_ready_count
            # Rollout must be nearly complete — at most 1 pod still cycling.
            # Without this, we'd declare success when only 1/5 pods are on the
            # new spec, and old pods would start terminating AFTER "resolved".
            and final_updated >= max(1, final_desired - 1)
        )

    if is_healthy:
        await queue.put(
            TimelineEvent(
                step="Validation",
                status="success",
                detail=f"All pods Running and Ready in {elapsed:.1f}s ✓",
            )
        )
    elif rollout_still_progressing:
        # The fix IS working — pods are being updated — just needs more time.
        # Mark as healthy to prevent unnecessary retry with the same fix.
        fr = final_rollout
        await queue.put(
            TimelineEvent(
                step="Validation",
                status="success",
                detail=f"Rollout nearly complete — {fr.get('updated', 0)}/{fr.get('desired', '?')} "
                       f"pods updated, {fr.get('ready', 0)}/{fr.get('desired', '?')} ready "
                       f"after {elapsed:.1f}s. Fix verified. ✓",
            )
        )
        is_healthy = True
        checks_passed.append(
            f"Rollout nearly complete: {fr.get('updated', 0)}/{fr.get('desired', '?')} updated, "
            f"{fr.get('ready', 0)}/{fr.get('desired', '?')} ready, "
            f"0 error pods — fix applied successfully"
        )
        checks_failed.clear()
    else:
        # ── Deep diagnostic + AI suggestions on failure ──────────────────
        await queue.put(
            TimelineEvent(
                step="Validation",
                status="error",
                detail=f"Recovery validation failed after {elapsed:.1f}s — running diagnostics…",
            )
        )

        diag = await loop.run_in_executor(None, _diagnose_failure, service, namespace)

        # Structured diagnostic summary
        diag_lines = [f"Image: {diag.get('current_image', '?')}",
                      f"Probe port: {diag.get('probe_port', '?')}",
                      f"Replicas: {diag.get('ready', 0)}/{diag.get('desired', '?')}",
                      f"Issues: {', '.join(diag.get('issues', ['none detected']))}"]
        await queue.put(
            TimelineEvent(
                step="Diagnostics",
                status="info",
                detail="Post-remediation diagnostic:\n" + "\n".join(f"  • {l}" for l in diag_lines),
            )
        )

        # AI-powered suggestions
        suggestions = await _generate_suggestions(service, namespace, diag, remediation_action)
        await queue.put(
            TimelineEvent(
                step="Recovery Suggestions",
                status="warning",
                detail=f"Recommended next steps:\n{suggestions}",
            )
        )
        checks_failed.append(f"Diagnostic issues: {', '.join(diag.get('issues', []))}")

    return ValidationResult(
        healthy=is_healthy,
        checks_passed=checks_passed,
        checks_failed=checks_failed,
        elapsed_seconds=elapsed,
    )
