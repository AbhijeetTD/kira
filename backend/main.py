"""KIRA — FastAPI application entry point.

Routes
------
POST /scan                    Manual infrastructure scan — analyse all workloads
POST /webhook/grafana         Receive Grafana alertmanager webhooks (automated)
GET  /incidents               List all incidents (summary)
GET  /incidents/{id}          Full incident detail
GET  /incidents/{id}/stream   Server-Sent Events — live investigation timeline
GET  /health                  Liveness + LLM reachability check
GET  /settings                Return current settings (tokens masked)
POST /settings                Update settings → write to .env + hot-reload
GET  /settings/kube-contexts  List available kubectl contexts
GET  /settings/ollama-models  List locally available Ollama models
POST /settings/test/jira      Test Jira API connectivity
POST /settings/test/teams     Test Teams webhook connectivity
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from backend.agent import investigator as inv_agent
from backend.agent import rca as rca_module  # kept for fallback
from backend.agent import remediation as rem_module
from backend.agent import validator as val_module
from backend.agent import war_room as war_room_module
from backend.agent import postmortem as postmortem_module
from backend.agent import chat as chat_module
from backend.agent import decision_engine
from backend.agent import outcome_tracker
from backend.config import settings
from backend.integrations import teams as teams_client
from backend.integrations import k8s_client as k8s
from backend.integrations import jira_client as jira
from backend.integrations.openai_client import check_health as llm_health
from backend.integrations.alert_parsers import parse_grafana_webhook
from backend.settings_routes import router as settings_router
from backend.models.incident import (
    AlertPayload,
    Incident,
    IncidentStatus,
    RemediationType,
    TimelineEvent,
)
from pydantic import BaseModel


class ChatRequest(BaseModel):
    question: str

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ── In-memory store (sufficient for hackathon demo) ────────────────────────
incidents: Dict[str, Incident] = {}

# ── Pending approvals: incident_id → {rca_result, evidence} ─────────────────
pending_approvals: Dict[str, dict] = {}


# ── App lifespan ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app_: FastAPI):
    logger.info("KIRA started — triggers: /scan (manual), /webhook/grafana (automated)")
    yield  # app runs here


app = FastAPI(
    title="KIRA",
    version="1.0.0",
    docs_url="/api/docs",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(settings_router)


# ── Emit queue — appends to incident timeline and notifies SSE waiters ──────

class _EmitQueue:
    """Thin wrapper around asyncio.Queue that also mirrors events into
    incident.timeline for SSE replay on late connections."""

    def __init__(self, incident: Incident) -> None:
        self._incident = incident

    async def put(self, item: TimelineEvent) -> None:
        self._incident.timeline.append(item)


_emit_queues: Dict[str, _EmitQueue] = {}


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    import subprocess
    llm_ok = await llm_health()
    # Resolve current cluster context
    try:
        ctx = subprocess.check_output(
            ["kubectl", "config", "current-context"], text=True
        ).strip()
    except Exception:
        ctx = "unknown"
    return {
        "status": "ok",
        "llm": "reachable" if llm_ok else "unreachable",
        "model": settings.ollama_model,
        "cluster_context": ctx,
        "active_incidents": len(
            [i for i in incidents.values() if i.status not in (
                IncidentStatus.RESOLVED, IncidentStatus.FAILED
            )]
        ),
    }


@app.get("/outcomes")
async def list_outcomes():
    """Return recent remediation outcome history for feedback/debugging."""
    history = outcome_tracker.get_history(limit=20)
    return [
        {
            "service": o.service,
            "namespace": o.namespace,
            "action": o.action,
            "pattern": o.pattern,
            "success": o.success,
            "confidence": o.confidence,
            "detail": o.detail,
            "timestamp": o.timestamp.isoformat(),
        }
        for o in history
    ]


# ── Infrastructure scan ─────────────────────────────────────────────────────

# System namespaces to always exclude from scans
_SYSTEM_NAMESPACES = {
    "kube-system", "kube-public", "kube-node-lease",
    "local-path-storage", "cert-manager", "ingress-nginx",
    "monitoring", "observability", "cattle-system",
}


async def _discover_namespaces() -> list[str]:
    """Return all non-system namespaces from the live cluster.
    Falls back to DEFAULT_NAMESPACE from settings if discovery fails."""
    loop = asyncio.get_event_loop()
    try:
        k8s._load_kube_config()
        core_v1 = k8s.client.CoreV1Api()
        ns_list = await loop.run_in_executor(None, core_v1.list_namespace)
        namespaces = [
            ns.metadata.name
            for ns in ns_list.items
            if ns.metadata.name not in _SYSTEM_NAMESPACES
        ]
        return namespaces or [settings.default_namespace]
    except Exception as exc:
        logger.warning("Namespace discovery failed, using default: %s", exc)
        return [settings.default_namespace]


@app.post("/scan", status_code=200)
async def scan_infrastructure(background_tasks: BackgroundTasks):
    """Scan all workloads across all non-system namespaces discovered from
    the live cluster. Creates incidents for unhealthy workloads."""
    loop = asyncio.get_event_loop()
    healthy = []
    unhealthy = []
    created_incidents = []

    scan_namespaces = await _discover_namespaces()
    logger.info("Scanning namespaces: %s", scan_namespaces)

    for ns in scan_namespaces:
        try:
            # List all deployments in the namespace
            k8s._load_kube_config()
            apps_v1 = k8s.client.AppsV1Api()

            deps = await loop.run_in_executor(
                None, apps_v1.list_namespaced_deployment, ns
            )
            for dep in deps.items:
                name = dep.metadata.name
                desired = dep.spec.replicas or 0
                ready = dep.status.ready_replicas or 0
                available = dep.status.available_replicas or 0
                unavailable = dep.status.unavailable_replicas or 0

                if ready == desired and unavailable == 0:
                    healthy.append({"name": name, "namespace": ns, "kind": "deployment",
                                    "replicas": f"{ready}/{desired}"})
                else:
                    unhealthy.append({"name": name, "namespace": ns, "kind": "deployment",
                                      "replicas": f"{ready}/{desired}",
                                      "unavailable": unavailable})

            # List all statefulsets in the namespace
            sts_list = await loop.run_in_executor(
                None, apps_v1.list_namespaced_stateful_set, ns
            )
            for sts in sts_list.items:
                name = sts.metadata.name
                desired = sts.spec.replicas or 0
                ready = sts.status.ready_replicas or 0

                if ready == desired:
                    healthy.append({"name": name, "namespace": ns, "kind": "statefulset",
                                    "replicas": f"{ready}/{desired}"})
                else:
                    unhealthy.append({"name": name, "namespace": ns, "kind": "statefulset",
                                      "replicas": f"{ready}/{desired}",
                                      "unavailable": desired - ready})

        except Exception as exc:
            logger.warning("Scan failed for namespace %s: %s", ns, exc)

    # Create incidents for unhealthy workloads
    for wl in unhealthy:
        # Skip if there's already an active incident
        already_active = any(
            inc.alert.service == wl["name"]
            and inc.alert.namespace == wl["namespace"]
            and inc.status not in (IncidentStatus.RESOLVED, IncidentStatus.FAILED, IncidentStatus.SKIPPED)
            for inc in incidents.values()
        )
        if already_active:
            continue

        payload = AlertPayload(
            service=wl["name"],
            namespace=wl["namespace"],
            message=(
                f"Infrastructure scan: {wl['kind']} '{wl['name']}' is unhealthy — "
                f"{wl['replicas']} replicas ready, {wl.get('unavailable', 0)} unavailable"
            ),
            severity="critical",
            source="infra-scan",
        )
        incident = Incident(alert=payload)
        incidents[incident.id] = incident
        _emit_queues[incident.id] = _EmitQueue(incident)
        background_tasks.add_task(run_agent, incident.id)
        created_incidents.append({"id": incident.id, "service": wl["name"], "namespace": wl["namespace"]})

    # If everything is healthy, create a resolved summary — no LLM calls needed
    if not unhealthy and not created_incidents:
        summary_payload = AlertPayload(
            service="infrastructure",
            namespace=scan_namespaces[0],
            message=(
                f"Infrastructure scan: all {len(healthy)} workloads healthy across "
                f"{', '.join(scan_namespaces)}"
            ),
            severity="info",
            source="infra-scan",
        )
        incident = Incident(alert=summary_payload)
        incident.status = IncidentStatus.RESOLVED
        incident.resolved_at = datetime.utcnow()
        incident.total_time_seconds = (
            incident.resolved_at - incident.started_at
        ).total_seconds()
        incidents[incident.id] = incident
        _emit_queues[incident.id] = _EmitQueue(incident)

        # Build a human-readable workload summary grouped by namespace
        ns_groups: Dict[str, list] = {}
        for wl in healthy:
            ns_groups.setdefault(wl["namespace"], []).append(wl)
        summary_lines = []
        for ns, wls in ns_groups.items():
            summary_lines.append(f"📁 {ns} — {len(wls)} workloads")
            for wl in wls:
                summary_lines.append(
                    f"  ✓ {wl['kind']}/{wl['name']}  {wl['replicas']} ready"
                )
        workload_detail = "\n".join(summary_lines)

        queue = _emit_queues[incident.id]
        await queue.put(TimelineEvent(
            step="Infrastructure Scan",
            status="success",
            detail=(
                f"Scanned {len(healthy)} workloads across "
                f"{', '.join(scan_namespaces)} — all healthy.\n\n"
                f"{workload_detail}"
            ),
        ))
        created_incidents.append({"id": incident.id, "service": "infrastructure",
                                  "namespace": scan_namespaces[0]})

    return {
        "scanned_namespaces": scan_namespaces,
        "total_workloads": len(healthy) + len(unhealthy),
        "healthy": len(healthy),
        "unhealthy": len(unhealthy),
        "healthy_workloads": healthy,
        "unhealthy_workloads": unhealthy,
        "incidents_created": created_incidents,
    }


@app.post("/webhook/grafana", status_code=202)
async def receive_grafana_alert(request: Request, background_tasks: BackgroundTasks):
    """Receive native Grafana alertmanager webhook.

    Configure in Grafana → Alerting → Contact Points → Webhook:
      URL: https://<your-host>/webhook/grafana
      Method: POST
    """
    body = await request.json()
    payloads = parse_grafana_webhook(body)
    if not payloads:
        return {"status": "ignored", "reason": "no firing alerts found"}

    created = []
    for payload in payloads:
        # Dedup check
        duplicate = False
        for inc in incidents.values():
            if (inc.alert.service == payload.service
                    and inc.alert.namespace == payload.namespace):
                # Skip if there's an active incident
                if inc.status not in (
                        IncidentStatus.RESOLVED,
                        IncidentStatus.FAILED,
                        IncidentStatus.SKIPPED,
                ):
                    duplicate = True
                    created.append({"incident_id": inc.id, "deduplicated": True})
                    break
                # Cooldown: suppress if resolved/failed within last 5 minutes
                if inc.resolved_at and (datetime.utcnow() - inc.resolved_at).total_seconds() < 300:
                    duplicate = True
                    created.append({"incident_id": inc.id, "deduplicated": True, "reason": "cooldown"})
                    break
        if duplicate:
            continue

        incident = Incident(alert=payload)
        incidents[incident.id] = incident
        _emit_queues[incident.id] = _EmitQueue(incident)
        background_tasks.add_task(run_agent, incident.id)
        logger.info(
            "Grafana webhook → incident %s for '%s' in '%s'",
            incident.id, payload.service, payload.namespace,
        )
        created.append({"incident_id": incident.id, "status": "investigating"})

    return {"incidents": created}


@app.get("/incidents")
async def list_incidents():
    return [
        {
            "id": inc.id,
            "service": inc.alert.service,
            "namespace": inc.alert.namespace,
            "message": inc.alert.message,
            "severity": inc.alert.severity,
            "status": inc.status,
            "started_at": inc.started_at.isoformat(),
            "resolved_at": (
                inc.resolved_at.isoformat() if inc.resolved_at else None
            ),
            "total_time_seconds": inc.total_time_seconds,
            "confidence": inc.rca.confidence if inc.rca else None,
            "root_cause": inc.rca.root_cause if inc.rca else None,
            "remediation_type": (
                inc.remediation.action if inc.remediation else None
            ),
            "jira_key": jira.get_ticket_key(inc.id),
            "jira_url": jira.get_ticket_url(inc.id),
            "source": inc.alert.source,
        }
        for inc in sorted(
            incidents.values(), key=lambda x: x.started_at, reverse=True
        )
    ]


@app.get("/metrics")
async def get_metrics():
    """Aggregated dashboard metrics derived from the in-memory incident store."""
    TERMINAL = {"resolved", "failed", "skipped"}
    all_inc = list(incidents.values())

    active_count   = sum(1 for i in all_inc if i.status not in TERMINAL)
    resolved_count = sum(1 for i in all_inc if i.status == "resolved")
    failed_count   = sum(1 for i in all_inc if i.status == "failed")
    total_count    = len(all_inc)

    resolved_with_time = [
        i for i in all_inc
        if i.status == "resolved" and i.total_time_seconds is not None
    ]
    avg_mttr = (
        round(sum(i.total_time_seconds for i in resolved_with_time) / len(resolved_with_time))
        if resolved_with_time else None
    )

    with_conf = [i for i in all_inc if i.rca and i.rca.confidence is not None]
    avg_confidence = (
        round(sum(i.rca.confidence for i in with_conf) / len(with_conf))
        if with_conf else None
    )

    auto_resolved = sum(
        1 for i in all_inc
        if i.status == "resolved" and getattr(i.alert, "source", None) != "manual"
    )

    return {
        "active_count":    active_count,
        "resolved_count":  resolved_count,
        "failed_count":    failed_count,
        "total_count":     total_count,
        "avg_mttr":        avg_mttr,
        "avg_confidence":  avg_confidence,
        "auto_resolved":   auto_resolved,
    }


@app.get("/incidents/{incident_id}")
async def get_incident(incident_id: str):
    if incident_id not in incidents:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incidents[incident_id].model_dump(mode="json")


@app.get("/incidents/{incident_id}/postmortem")
async def get_postmortem(incident_id: str):
    """Generate (and cache) a structured markdown postmortem for an incident."""
    if incident_id not in incidents:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident = incidents[incident_id]
    if not incident.postmortem:
        incident.postmortem = await postmortem_module.generate_postmortem(incident)
    return {"postmortem": incident.postmortem}


@app.post("/incidents/{incident_id}/chat")
async def chat_with_sherlock(incident_id: str, body: ChatRequest):
    """Ask KIRA a question about a specific incident."""
    if incident_id not in incidents:
        raise HTTPException(status_code=404, detail="Incident not found")
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question must not be empty")
    answer = await chat_module.answer_question(incidents[incident_id], question)
    return {"answer": answer}


@app.post("/incidents/{incident_id}/action")
async def incident_action(incident_id: str, request: Request):
    """UI-driven approve/skip — mirrors the Teams button callback but from the dashboard."""
    if incident_id not in incidents:
        raise HTTPException(status_code=404, detail="Incident not found")

    body = await request.json()
    action = body.get("action")  # "approve" or "skip"

    incident = incidents[incident_id]

    if action == "approve":
        if incident_id not in pending_approvals:
            raise HTTPException(status_code=409, detail="Incident is not awaiting approval")
        pending = pending_approvals.pop(incident_id)
        asyncio.create_task(
            _execute_and_validate(incident_id, pending["rca_result"], pending["evidence"],
                                 pending.get("decision_pattern", "gpt_engine"))
        )
        return {"ok": True, "action": "approved"}

    elif action == "skip":
        pending_approvals.pop(incident_id, None)
        incident.status = IncidentStatus.SKIPPED
        incident.resolved_at = datetime.utcnow()
        incident.total_time_seconds = (
            incident.resolved_at - incident.started_at
        ).total_seconds()
        queue = _emit_queues.get(incident_id)
        if queue:
            await queue.put(
                TimelineEvent(
                    step="Remediation",
                    status="info",
                    detail="Skipped by operator via dashboard — no remediation action taken.",
                )
            )
        return {"ok": True, "action": "skipped"}

    raise HTTPException(status_code=400, detail="action must be 'approve' or 'skip'")


@app.get("/incidents/{incident_id}/stream")
async def stream_incident(incident_id: str):
    """Server-Sent Events endpoint.  The frontend polls this; events are
    pushed as soon as they are appended to incident.timeline."""
    if incident_id not in incidents:
        raise HTTPException(status_code=404, detail="Incident not found")

    async def event_generator():
        incident = incidents[incident_id]
        pos = 0  # next index to send from incident.timeline

        while True:
            # Flush any new timeline events
            while pos < len(incident.timeline):
                event = incident.timeline[pos]
                yield {"data": json.dumps(event.model_dump(mode="json"))}
                pos += 1

            # Terminal states — send done signal and exit
            if incident.status in (
                IncidentStatus.RESOLVED,
                IncidentStatus.FAILED,
                IncidentStatus.SKIPPED,
            ):
                yield {
                    "data": json.dumps(
                        {
                            "type": "done",
                            "status": incident.status,
                            "total_time_seconds": incident.total_time_seconds,
                        }
                    )
                }
                break

            # Short poll — low latency, zero extra dependencies
            await asyncio.sleep(0.4)

    return EventSourceResponse(event_generator())


# ── Shared phases 3+4: remediation + validation + finalise ──────────────────

async def _execute_and_validate(incident_id: str, rca_result, evidence,
                                decision_pattern: str = "gpt_engine",
                                attempt: int = 1,
                                max_attempts: int = 2) -> None:
    """Runs remediation → validation → close.
    Called from run_agent (auto-mode) or from /teams/action (approval-mode).
    On failure, retries once with re-gathered evidence and escalated sizing."""
    incident = incidents[incident_id]
    queue = _emit_queues[incident_id]
    service = incident.alert.service
    namespace = incident.alert.namespace

    async def emit(step: str, status: str, detail: str) -> None:
        await queue.put(TimelineEvent(step=step, status=status, detail=detail))

    try:
        # Phase 3: Remediation
        incident.status = IncidentStatus.REMEDIATING
        plan = await rem_module.execute(
            rca_result, service, namespace, queue, evidence,
            decision_pattern=decision_pattern,
        )
        incident.remediation = plan

        # Jira: log remediation execution
        if plan.executed:
            await jira.on_remediation_executed(
                incident_id, plan.command or "", plan.output or "", plan.success,
            )

        # Detect if the remediation actually changed anything
        remediation_failed = plan.executed and not plan.success
        no_change = plan.executed and plan.success and plan.output and "no change" in plan.output.lower()

        if remediation_failed:
            await emit(
                "Remediation",
                "error",
                f"Remediation command failed: {plan.output[:300]}",
            )
        elif no_change:
            await emit(
                "Remediation",
                "warning",
                "Command executed but nothing changed — the resource already had the target values. "
                "The AI diagnosis may have been inaccurate.",
            )

        # Phase 4: Recovery validation (with diagnostic on failure)
        incident.status = IncidentStatus.VALIDATING
        val_result = await val_module.validate_recovery(
            service, namespace, queue,
            remediation_action=rca_result.remediation_type.value,
        )
        incident.validation = val_result

        # Finalise
        # If remediation failed or was a no-op, don't claim resolved
        if remediation_failed or no_change:
            actually_fixed = False
        else:
            actually_fixed = val_result.healthy

        # ── Retry on failure ─────────────────────────────────────────────
        if not actually_fixed and attempt < max_attempts:
            await emit(
                "Retry",
                "warning",
                f"Attempt {attempt} failed — re-gathering evidence and retrying "
                f"with escalated approach (attempt {attempt + 1}/{max_attempts}).",
            )
            # Re-gather fresh evidence
            retry_evidence = await inv_agent.gather_evidence(service, namespace, queue)
            incident.evidence = retry_evidence

            # Re-run GPT decision engine on fresh evidence
            try:
                retry_decision, retry_rca = await decision_engine.evaluate(
                    retry_evidence, service, namespace,
                    incident.alert.message, incident.agent_opinions, queue,
                )
                retry_pattern = retry_decision.pattern
                await emit(
                    "Retry — Decision Engine",
                    "info",
                    f"GPT Decision Engine → {retry_decision.remediation_type.value}. "
                    f"Confidence: {retry_decision.confidence}%. "
                    f"Reason: {retry_decision.reason}",
                )
            except Exception:
                # GPT decision engine failed on retry — use fallback logic
                if remediation_failed:
                    info = decision_engine._get_deployment_info(retry_evidence)
                    ctx = decision_engine._kube_context_flag()
                    wl_kind = info.get('kind', 'deployment')
                    rollback_cmd = f"kubectl rollout undo {wl_kind}/{info.get('deployment', service)} -n {namespace}{ctx}"
                    retry_rca = rca_result.model_copy(update={
                        "remediation_type": RemediationType.ROLLBACK,
                        "confidence": 80,
                        "remediation_command": rollback_cmd,
                        "remediation_reason": "Previous remediation failed — falling back to rollback.",
                    })
                    retry_pattern = "retry_rollback"
                    await emit("Retry — Fallback", "info", "GPT retry failed — falling back to rollback.")
                elif rca_result.remediation_type == RemediationType.PATCH:
                    info = decision_engine._get_deployment_info(retry_evidence)
                    ctx = decision_engine._kube_context_flag()
                    wl_kind = info.get('kind', 'deployment')
                    rollback_cmd = f"kubectl rollout undo {wl_kind}/{info.get('deployment', service)} -n {namespace}{ctx}"
                    retry_rca = rca_result.model_copy(update={
                        "remediation_type": RemediationType.ROLLBACK,
                        "confidence": 80,
                        "remediation_command": rollback_cmd,
                        "remediation_reason": "Resource patch did not restore health — escalating to rollback.",
                        "root_cause": "Resource patch did not restore health — escalating to rollback.",
                    })
                    retry_pattern = "retry_escalate_rollback"
                    await emit("Retry — Escalate", "info", "Patch did not restore health — escalating to rollback.")
                else:
                    retry_rca = rca_result
                    retry_pattern = decision_pattern
                    await emit("Retry — Re-evaluate", "info", "GPT retry failed — retrying with same approach.")

            return await _execute_and_validate(
                incident_id, retry_rca, retry_evidence,
                decision_pattern=retry_pattern,
                attempt=attempt + 1,
                max_attempts=max_attempts,
            )

        # ── Record outcome for feedback loop ─────────────────────────────
        outcome_tracker.record(
            service=service,
            namespace=namespace,
            action=rca_result.remediation_type.value,
            pattern=decision_pattern,
            success=actually_fixed,
            confidence=rca_result.confidence,
            detail=rca_result.root_cause[:200] if rca_result.root_cause else "",
        )

        incident.status = (
            IncidentStatus.RESOLVED if actually_fixed else IncidentStatus.FAILED
        )
        incident.resolved_at = datetime.utcnow()
        incident.total_time_seconds = (
            incident.resolved_at - incident.started_at
        ).total_seconds()

        elapsed_str = f"{incident.total_time_seconds:.0f}s"
        final_status = "success" if actually_fixed else "error"
        resolution_word = "RESOLVED" if actually_fixed else "UNRESOLVED"

        extra_note = ""
        if remediation_failed:
            extra_note = "  |  Note: Remediation command failed"
        elif no_change:
            extra_note = "  |  Note: No actual change was made — diagnosis may be inaccurate"

        await emit(
            "Incident Closed",
            final_status,
            f"Incident {resolution_word} in {elapsed_str}. "
            f"Confidence: {rca_result.confidence}%  |  "
            f"Action: {plan.command if plan.executed else 'none'}{extra_note}",
        )

        actions = (
            plan.command
            if plan.executed
            else "No autonomous action taken (low confidence or none required)."
        )
        await teams_client.post_incident_summary(
            incident_id,
            rca_result.root_cause,
            actions,
            elapsed_str,
            confidence=rca_result.confidence,
        )

        # Jira: close out ticket
        if actually_fixed:
            await jira.on_incident_resolved(
                incident_id, incident.total_time_seconds,
                plan.command if plan.executed else "none",
            )
        else:
            await jira.on_incident_failed(incident_id, rca_result.root_cause or "Unknown")

    except Exception as exc:  # noqa: BLE001
        logger.exception("_execute_and_validate failed for incident %s", incident_id)
        incident.status = IncidentStatus.FAILED
        await emit("Pipeline Error", "error", str(exc))
        await jira.on_incident_failed(incident_id, str(exc))


# ── Agent pipeline ────────────────────────────────────────────────────────────

async def run_agent(incident_id: str) -> None:
    incident = incidents[incident_id]
    queue = _emit_queues[incident_id]
    service = incident.alert.service
    namespace = incident.alert.namespace

    async def emit(step: str, status: str, detail: str) -> None:
        await queue.put(TimelineEvent(step=step, status=status, detail=detail))

    try:
        incident.status = IncidentStatus.INVESTIGATING
        await teams_client.post_alert_received(
            incident_id, service, incident.alert.message
        )
        # Jira: create ticket on incident start
        jira_key = await jira.on_incident_created(
            incident_id, service, namespace,
            incident.alert.message, incident.alert.severity,
        )
        if jira_key:
            await emit("Jira", "info", f"Ticket created: {jira_key}")
        await emit(
            "Investigation Started",
            "info",
            f"Analysing service '{service}' in namespace '{namespace}'",
        )
        # Emit alert source so frontend can display it
        await emit(
            "Alert Received",
            "info",
            f"Alert triggered from {incident.alert.source}",
        )

        # ── Phase 1: Evidence gathering ──────────────────────────────────
        evidence = await inv_agent.gather_evidence(service, namespace, queue)
        incident.evidence = evidence

        # ── Phase 2: Multi-Agent War Room (specialists only) ──────────────
        agent_opinions = await war_room_module.run_war_room(
            service, namespace, incident.alert.message, evidence, queue
        )
        incident.agent_opinions = agent_opinions

        # ── Phase 3: GPT Decision Engine (replaces Judge + old deterministic) ─
        incident.status = IncidentStatus.RCA_COMPLETE
        det_decision, rca_result = await decision_engine.evaluate(
            evidence, service, namespace, incident.alert.message,
            agent_opinions, queue,
        )
        incident.rca = rca_result
        decision_pattern = det_decision.pattern

        # Jira: update with RCA results
        await jira.on_rca_complete(
            incident_id, rca_result.root_cause, rca_result.confidence,
            rca_result.remediation_type.value,
            rca_result.remediation_command or "",
        )

        # ── Check outcome history — avoid repeating failed actions ────────
        last_failed = outcome_tracker.last_failed_action(service, namespace)
        if last_failed and last_failed == rca_result.remediation_type.value:
            await emit(
                "Decision Engine",
                "warning",
                f"'{last_failed}' previously failed for {service}/{namespace} — "
                f"this may not resolve the issue. Consider manual intervention.",
            )

        # ── Phase 4+5: auto-execute OR wait for approval ─────────────────
        # If the decision engine determined no action needed, verify pods are actually healthy
        if rca_result.remediation_type == RemediationType.NONE:
            pod_status = (evidence.pod_status or "").lower()
            failing_states = ("imagepullbackoff", "errimagepull", "crashloopbackoff",
                              "containercreating", "pending", "error", "terminated")
            has_failing_pods = any(state in pod_status for state in failing_states)

            # Also detect stuck rollouts: pods that are Running but not fully Ready
            # e.g. "3/4 Ready" means a container is failing its readiness probe
            import re as _re
            not_fully_ready = bool(_re.search(
                r'(\d+)/(\d+)\s+Ready',
                evidence.pod_status or "",
            ) and any(
                m.group(1) != m.group(2)
                for m in _re.finditer(r'(\d+)/(\d+)\s+Ready', evidence.pod_status or "")
            ))
            has_failing_pods = has_failing_pods or not_fully_ready

            incident.resolved_at = datetime.utcnow()
            incident.total_time_seconds = (
                incident.resolved_at - incident.started_at
            ).total_seconds()
            elapsed_str = f"{incident.total_time_seconds:.0f}s"

            if has_failing_pods:
                # GPT said "none" but pods are unhealthy — mark as failed, not resolved
                incident.status = IncidentStatus.FAILED
                await emit(
                    "Incident Closed",
                    "error",
                    f"Decision Engine recommended no action, but unhealthy pods remain. "
                    f"Manual intervention required. Closed in {elapsed_str}. "
                    f"{rca_result.remediation_reason}",
                )
            else:
                incident.status = IncidentStatus.RESOLVED
                await emit(
                    "Incident Closed",
                    "success",
                    f"No remediation needed — deployment is healthy. "
                    f"Closed in {elapsed_str}. {rca_result.remediation_reason}",
                )
            return

        # Rule: confidence > auto_approve_threshold → auto-fix immediately
        #        confidence <= auto_approve_threshold → require human approval
        auto_approve = rca_result.confidence > settings.auto_approve_threshold

        if auto_approve:
            await emit(
                "Auto-Approved",
                "info",
                f"Confidence {rca_result.confidence}% > {settings.auto_approve_threshold}% — "
                f"proceeding with autonomous remediation.",
            )
            await _execute_and_validate(incident_id, rca_result, evidence, decision_pattern)
        else:
            pending_approvals[incident_id] = {
                "rca_result": rca_result,
                "evidence": evidence,
                "decision_pattern": decision_pattern,
            }
            incident.status = IncidentStatus.AWAITING_APPROVAL
            cmd_preview = rca_result.remediation_command or rca_result.remediation_type.value
            cmd_display_approval = cmd_preview.replace("{}", namespace) if cmd_preview else ""
            await emit(
                "Awaiting Approval",
                "warning",
                f"⚠️ Confidence {rca_result.confidence}% ≤ {settings.auto_approve_threshold}% — "
                f"human approval required before executing: {rca_result.remediation_type.value}. "
                f"Command: {cmd_display_approval}  |  "
                f"Root cause: {rca_result.root_cause[:300]}",
            )
            await teams_client.post_approval_request(
                incident_id=incident_id,
                service=service,
                namespace=namespace,
                root_cause=rca_result.root_cause,
                confidence=rca_result.confidence,
                recommended_action=rca_result.remediation_type,
                command=cmd_preview,
            )

    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent pipeline failed for incident %s", incident_id)
        incident.status = IncidentStatus.FAILED
        await emit("Pipeline Error", "error", str(exc))


# ── Teams interactive button callback ─────────────────────────────────────────

@app.post("/teams/action")
async def teams_action(request: Request):
    """Receives Teams button clicks (Approve / Skip) for approval-mode incidents.

    Teams MessageCard HttpPOST actions deliver a JSON body of the exact form
    specified in the card definition, e.g. {"action": "approve"}.
    The incident_id is passed as a query-param: /teams/action?incident_id=<id>.
    """
    body = await request.body()

    # Teams HttpPOST actions send JSON directly — read action + incident_id
    import json as _json_teams  # noqa: PLC0415
    try:
        data = _json_teams.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    action_id_val = data.get("action")              # "approve" or "skip"
    incident_id   = request.query_params.get("incident_id", "")

    # Shim: build a synthetic form dict so the shared logic below works
    form = {}  # not used in Teams path
    if not incident_id or incident_id not in incidents:
        raise HTTPException(status_code=404, detail="Incident not found")

    incident = incidents[incident_id]

    if action_id_val == "approve":
        if incident_id not in pending_approvals:
            return JSONResponse({"ok": True, "detail": "Not awaiting approval"})

        pending = pending_approvals.pop(incident_id)
        # Resume the pipeline in the background
        asyncio.create_task(
            _execute_and_validate(incident_id, pending["rca_result"], pending["evidence"],
                                 pending.get("decision_pattern", "gpt_engine"))
        )

    elif action_id_val == "skip":
        pending_approvals.pop(incident_id, None)
        incident.status = IncidentStatus.SKIPPED
        incident.resolved_at = datetime.utcnow()
        incident.total_time_seconds = (
            incident.resolved_at - incident.started_at
        ).total_seconds()
        queue = _emit_queues.get(incident_id)
        if queue:
            await queue.put(
                TimelineEvent(
                    step="Remediation",
                    status="info",
                    detail="Skipped via Teams approval card — no remediation action taken.",
                )
            )
    else:
        raise HTTPException(status_code=400, detail="action must be 'approve' or 'skip'")

    return JSONResponse({"ok": True})


# ── Static frontend ───────────────────────────────────────────────────────────
_frontend_dir = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "frontend"
)
if os.path.isdir(_frontend_dir):
    app.mount(
        "/", StaticFiles(directory=_frontend_dir, html=True), name="frontend"
    )
