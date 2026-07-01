"""Evidence gathering — runs all Kubernetes data collection steps and emits
timeline events for each one."""
from __future__ import annotations

import asyncio
import logging

from backend.integrations import k8s_client
from backend.models.incident import Evidence, TimelineEvent

logger = logging.getLogger(__name__)


async def gather_evidence(
    service: str,
    namespace: str,
    queue: object,  # duck-typed: must have async put(TimelineEvent)
) -> Evidence:
    evidence = Evidence()
    loop = asyncio.get_event_loop()

    async def step(
        display_name: str,
        evidence_field: str,
        fn,
        *args,
    ) -> None:
        await queue.put(
            TimelineEvent(
                step=display_name,
                status="running",
                detail=f"Collecting {display_name.lower()}...",
            )
        )
        try:
            result: str = await loop.run_in_executor(None, fn, *args)
            setattr(evidence, evidence_field, result)
            preview = (result[:1200] + "…") if len(result) > 1200 else result
            await queue.put(
                TimelineEvent(step=display_name, status="success", detail=preview)
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Investigation step '%s' failed", display_name)
            await queue.put(
                TimelineEvent(step=display_name, status="error", detail=str(exc))
            )

    await step("Pod Status",          "pod_status",          k8s_client.get_pod_status,             namespace, service)
    await step("Pod Logs",            "pod_logs",            k8s_client.get_logs_for_deployment,    service, namespace)
    await step("Resource Usage",      "resource_usage",      k8s_client.get_resource_usage,         namespace)
    await step("Rollout History",     "rollout_history",     k8s_client.get_rollout_history,        service, namespace)
    await step("Deployment Describe", "deployment_describe", k8s_client.get_deployment_describe,    service, namespace)
    await step("Recent Events",       "recent_events",       k8s_client.get_recent_events,          namespace, service)
    await step("Correlated Services", "correlated_services", k8s_client.get_correlated_service_logs, service, namespace)

    # Structured deployment data for the decision engine (no string parsing needed)
    try:
        evidence.deployment_info = await loop.run_in_executor(
            None, k8s_client.get_deployment_info, service, namespace
        )
    except Exception:  # noqa: BLE001
        logger.warning("Failed to collect structured deployment info")

    return evidence
