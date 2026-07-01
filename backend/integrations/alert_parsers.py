"""Parsers for external alert sources (Grafana, OpsGenie).

Each parser normalises the incoming payload into the internal AlertPayload
format so the KIRA pipeline can process it uniformly.
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional

from backend.models.incident import AlertPayload

logger = logging.getLogger(__name__)


# ── Grafana Alertmanager webhook format ──────────────────────────────────────
# Docs: https://grafana.com/docs/grafana/latest/alerting/configure-notifications/manage-contact-points/integrations/webhook-notifier/

def parse_grafana_webhook(body: dict) -> List[AlertPayload]:
    """Parse a Grafana alertmanager webhook payload into AlertPayload(s).

    Grafana sends:
    {
      "status": "firing",
      "alerts": [
        {
          "status": "firing",
          "labels": {"alertname": "...", "namespace": "...", "pod": "...", ...},
          "annotations": {"summary": "..."},
          ...
        }
      ]
    }
    """
    alerts = body.get("alerts", [])
    if not alerts:
        # Single-alert format (some older Grafana versions)
        alerts = [body] if body.get("labels") else []

    results: List[AlertPayload] = []
    for alert in alerts:
        status = alert.get("status", "firing")
        if status != "firing":
            continue  # Only process firing alerts

        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})

        namespace = labels.get("namespace", "unknown")
        pod = labels.get("pod", "")
        alertname = labels.get("alertname", "Unknown Alert")

        # Skip Grafana internal/meta alerts (e.g. DatasourceNoData, DatasourceError)
        if alertname.startswith("Datasource") or alertname in ("GrafanaAlert",):
            logger.debug("Skipping Grafana internal alert: %s", alertname)
            continue

        severity = labels.get("severity", labels.get("og_priority", "warning"))
        cluster = labels.get("clustername", "")
        product = labels.get("product", "")

        # Derive service name from pod (strip replicaset hash suffix)
        # Fall back to deployment/statefulset label for resource-level alerts
        if pod:
            service = _pod_to_service(pod)
        elif labels.get("deployment"):
            service = labels["deployment"]
        elif labels.get("statefulset"):
            service = labels["statefulset"]
        else:
            service = labels.get("service", alertname)

        # Build descriptive message
        summary = annotations.get("summary", annotations.get("description", ""))
        message = summary or f"{alertname} — pod {pod} in {namespace}"

        # Map severity
        severity_mapped = _map_severity(severity)

        source_parts = ["grafana"]
        if cluster:
            source_parts.append(cluster)
        if product:
            source_parts.append(product)

        results.append(AlertPayload(
            service=service,
            namespace=namespace,
            message=message,
            severity=severity_mapped,
            source=" | ".join(source_parts),
        ))

    return results


# ── OpsGenie webhook format ──────────────────────────────────────────────────
# Docs: https://support.atlassian.com/opsgenie/docs/opsgenie-edge-connector-alert-action-data/

def parse_opsgenie_webhook(body: dict) -> Optional[AlertPayload]:
    """Parse an OpsGenie (Jira Service Management) webhook payload.

    OpsGenie sends:
    {
      "action": "Create",   // or "Acknowledge", "Close", etc.
      "alert": {
        "alertId": "...",
        "message": "P3 [Grafana]: ...",
        "tags": ["alertSource:Grafana", "namespace:...", ...],
        "priority": "P3",
        "description": "...",
        ...
      }
    }

    Also handles the newer JSM/Atlassian format.
    """
    action = body.get("action", "").lower()

    # Only process new alerts (Create) — ignore Ack, Close, etc.
    if action not in ("create", ""):
        logger.info("OpsGenie action '%s' ignored (not a create)", action)
        return None

    alert_data = body.get("alert", body)  # fallback to body itself
    message = alert_data.get("message", alert_data.get("description", ""))
    tags = alert_data.get("tags", [])
    priority = alert_data.get("priority", "P3")
    description = alert_data.get("description", "")

    # Parse tags: OpsGenie sends tags as ["key:value", ...]
    tag_map = _parse_opsgenie_tags(tags)

    namespace = tag_map.get("namespace", "unknown")
    pod = tag_map.get("pod", "")
    alertname = tag_map.get("alertname", "")
    severity = tag_map.get("severity", priority)
    cluster = tag_map.get("clustername", "")
    product = tag_map.get("product", "")

    # Derive service from pod name
    service = _pod_to_service(pod) if pod else tag_map.get("service", alertname or "unknown")

    # Use description for richer context, fall back to message
    full_message = description if description else message

    # Map priority to severity
    severity_mapped = _map_severity(severity)

    source_parts = ["opsgenie"]
    if cluster:
        source_parts.append(cluster)
    if product:
        source_parts.append(product)

    return AlertPayload(
        service=service,
        namespace=namespace,
        message=full_message[:2000],  # truncate overly long descriptions
        severity=severity_mapped,
        source=" | ".join(source_parts),
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _pod_to_service(pod_name: str) -> str:
    """Extract service/deployment name from a pod name.

    Examples:
      data-admin-api-5cd667998d-z9srx → data-admin-api
      cart-web-7f4b8c9d6-abc12        → cart-web
      my-service-abc123def4-xyz       → my-service
    """
    # Pattern: <name>-<replicaset-hash>-<pod-hash>
    match = re.match(r"^(.+)-[a-f0-9]{8,10}-[a-z0-9]{5}$", pod_name)
    if match:
        return match.group(1)
    # Pattern: <name>-<pod-hash> (StatefulSet or DaemonSet)
    match = re.match(r"^(.+)-[a-z0-9]{5}$", pod_name)
    if match:
        return match.group(1)
    # Fallback — strip trailing hash-like segments
    match = re.match(r"^(.+?)-[a-f0-9]+$", pod_name)
    if match:
        return match.group(1)
    return pod_name


def _parse_opsgenie_tags(tags) -> dict:
    """Parse OpsGenie tag list ['key:value', ...] into a dict."""
    result = {}
    if not tags:
        return result
    for tag in tags:
        if isinstance(tag, str) and ":" in tag:
            key, _, value = tag.partition(":")
            result[key.strip()] = value.strip()
    return result


def _map_severity(raw: str) -> str:
    """Normalise severity/priority strings to internal severity levels."""
    raw_lower = raw.lower().strip()
    if raw_lower in ("p1", "critical", "crit"):
        return "critical"
    if raw_lower in ("p2", "high", "error"):
        return "high"
    if raw_lower in ("p3", "medium", "warn", "warning"):
        return "warning"
    if raw_lower in ("p4", "p5", "low", "info", "informational"):
        return "low"
    return "warning"
