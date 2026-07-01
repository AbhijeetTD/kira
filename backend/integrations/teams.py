"""Outbound Microsoft Teams notifications via an incoming webhook URL.

Teams uses the MessageCard JSON format for connector cards:
https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/connectors-using

Approval buttons use the HttpPOST potentialAction so that clicking
Approve / Skip in Teams directly calls POST /incidents/{id}/action —
the same endpoint used by the dashboard UI.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)


async def _post_card(payload: dict) -> bool:
    """Send a MessageCard payload to the configured Teams webhook URL."""
    if not settings.teams_webhook_url:
        logger.info("Teams webhook not configured — skipping notification.")
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            response = await http.post(
                settings.teams_webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            # Teams returns HTTP 200 with body "1" on success
            if response.status_code == 200:
                return True
            logger.debug(
                "Teams responded %s: %s", response.status_code, response.text
            )
            return False
    except httpx.RequestError as exc:
        logger.error("Teams post failed: %s", exc)
        return False


async def post_message(text: str) -> bool:
    """Send a plain-text Teams notification."""
    payload = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "themeColor": "0076D7",
        "summary": text,
        "sections": [{"text": text}],
    }
    return await _post_card(payload)


async def post_alert_received(
    incident_id: str, service: str, message: str
) -> None:
    """Notify Teams that KIRA has started investigating an incident."""
    payload = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "themeColor": "0076D7",
        "summary": f"KIRA investigating incident {incident_id}",
        "sections": [
            {
                "activityTitle": "🔍 KIRA — Investigation Started",
                "activitySubtitle": f"Incident `{incident_id}` on **{service}**",
                "facts": [
                    {"name": "Service", "value": service},
                    {"name": "Incident ID", "value": incident_id},
                    {"name": "Alert", "value": message},
                ],
            }
        ],
    }
    await _post_card(payload)


async def post_approval_request(
    incident_id: str,
    service: str,
    namespace: str,
    root_cause: str,
    confidence: int,
    recommended_action: str,
    command: str,
) -> None:
    """Post a Teams MessageCard with Approve / Skip action buttons.

    Requires TEAMS_WEBHOOK_URL and the server to be reachable at PUBLIC_URL
    so that Teams can deliver the HttpPOST button callbacks to
    POST /incidents/{id}/action.
    """
    approve_url = f"{settings.public_url}/incidents/{incident_id}/action"
    payload = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "themeColor": "FF8C00",
        "summary": f"KIRA — Approval Required for incident {incident_id}",
        "sections": [
            {
                "activityTitle": "⚠️ KIRA — Approval Required",
                "activitySubtitle": f"Incident **{incident_id}** on **{service}**",
                "facts": [
                    {"name": "Service", "value": service},
                    {"name": "Namespace", "value": namespace},
                    {"name": "Confidence", "value": f"{confidence}%"},
                    {"name": "Recommended Action", "value": recommended_action},
                    {"name": "Command", "value": f"`{command}`"},
                ],
            },
            {
                "activityTitle": "Root Cause",
                "text": root_cause[:500],
            },
        ],
        "potentialAction": [
            {
                "@type": "HttpPOST",
                "name": "✅ Approve Remediation",
                "target": approve_url,
                "body": '{"action": "approve"}',
                "headers": [
                    {"name": "Content-Type", "value": "application/json"}
                ],
            },
            {
                "@type": "HttpPOST",
                "name": "❌ Skip",
                "target": approve_url,
                "body": '{"action": "skip"}',
                "headers": [
                    {"name": "Content-Type", "value": "application/json"}
                ],
            },
        ],
    }
    await _post_card(payload)


async def post_incident_summary(
    incident_id: str,
    root_cause: str,
    actions: str,
    resolved_in: str,
    confidence: int = 0,
) -> None:
    """Post a resolved/summary card to Teams."""
    payload = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "themeColor": "00B050",
        "summary": f"KIRA resolved incident {incident_id} in {resolved_in}",
        "sections": [
            {
                "activityTitle": f"✅ KIRA — Incident {incident_id} Resolved",
                "activitySubtitle": f"Resolved in **{resolved_in}**",
                "facts": [
                    {"name": "Resolved In", "value": resolved_in},
                    {"name": "Confidence", "value": f"{confidence}%"},
                    {"name": "Actions Taken", "value": actions},
                ],
            },
            {
                "activityTitle": "Root Cause",
                "text": root_cause,
            },
        ],
    }
    await _post_card(payload)
