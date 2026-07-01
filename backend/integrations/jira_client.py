"""Jira Cloud integration for KIRA — full incident lifecycle.

Creates and updates Jira tickets as incidents progress through the pipeline:
  1. Alert received → Create ticket (To Do)
  2. RCA complete → Add comment with root cause + transition to In Progress
  3. Remediation executed → Add comment with command + output
  4. Resolved/Failed → Add comment with outcome + transition to Done/reopen

Uses Jira REST API v3 with Basic Auth (email + API token).
Configure via env vars: JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)

# ── In-memory mapping: incident_id → jira_issue_key ──────────────────────────
_incident_to_jira: dict[str, str] = {}


def _auth() -> tuple[str, str] | None:
    if not settings.jira_url or not settings.jira_email or not settings.jira_api_token:
        return None
    return (settings.jira_email, settings.jira_api_token)


def _base_url() -> str:
    url = settings.jira_url.rstrip("/")
    if not url.startswith("http"):
        url = f"https://{url}"
    return url


def _headers() -> dict:
    return {"Content-Type": "application/json", "Accept": "application/json"}


def is_configured() -> bool:
    return bool(settings.jira_url and settings.jira_email and settings.jira_api_token)


# ── Create ticket ─────────────────────────────────────────────────────────────

async def create_incident_ticket(
    incident_id: str,
    service: str,
    namespace: str,
    message: str,
    severity: str = "critical",
) -> Optional[str]:
    """Create a Jira ticket for a new incident. Returns the issue key (e.g. KS-42)."""
    if not is_configured():
        logger.debug("Jira not configured — skipping ticket creation.")
        return None

    priority_map = {"critical": "High", "warning": "Medium", "info": "Low"}
    priority = priority_map.get(severity, "Medium")

    payload = {
        "fields": {
            "project": {"key": settings.jira_project_key},
            "summary": f"[KIRA] {service} — {_short_alert(message)}",
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": f"Incident ID: {incident_id}"},
                        ],
                    },
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": f"Service: {service}"},
                        ],
                    },
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": f"Namespace: {namespace}"},
                        ],
                    },
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": f"Alert: {message}"},
                        ],
                    },
                ],
            },
            "issuetype": {"name": settings.jira_issue_type},
            "priority": {"name": priority},
            "labels": ["kira", "auto-detected", namespace],
        }
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(
                f"{_base_url()}/rest/api/3/issue",
                json=payload,
                auth=_auth(),
                headers=_headers(),
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                issue_key = data["key"]
                _incident_to_jira[incident_id] = issue_key
                logger.info("Jira ticket created: %s for incident %s", issue_key, incident_id)
                return issue_key
            else:
                logger.error("Jira create failed (%d): %s", resp.status_code, resp.text[:500])
                return None
    except httpx.RequestError as exc:
        logger.error("Jira create request error: %s", exc)
        return None


# ── Add comment ──────────────────────────────────────────────────────────────

async def add_comment(incident_id: str, comment_text: str) -> bool:
    """Add a comment to the Jira ticket associated with an incident."""
    if not is_configured():
        return False

    issue_key = _incident_to_jira.get(incident_id)
    if not issue_key:
        logger.debug("No Jira ticket for incident %s — skipping comment.", incident_id)
        return False

    payload = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": comment_text}],
                }
            ],
        }
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(
                f"{_base_url()}/rest/api/3/issue/{issue_key}/comment",
                json=payload,
                auth=_auth(),
                headers=_headers(),
            )
            if resp.status_code in (200, 201):
                logger.info("Jira comment added to %s", issue_key)
                return True
            else:
                logger.error("Jira comment failed (%d): %s", resp.status_code, resp.text[:300])
                return False
    except httpx.RequestError as exc:
        logger.error("Jira comment error: %s", exc)
        return False


# ── Transition ticket ─────────────────────────────────────────────────────────

async def transition_ticket(incident_id: str, target_status: str) -> bool:
    """Transition a Jira ticket to a new status (e.g., 'In Progress', 'Done').

    Looks up available transitions and picks the one matching target_status.
    """
    if not is_configured():
        return False

    issue_key = _incident_to_jira.get(incident_id)
    if not issue_key:
        return False

    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            # Get available transitions
            resp = await http.get(
                f"{_base_url()}/rest/api/3/issue/{issue_key}/transitions",
                auth=_auth(),
                headers=_headers(),
            )
            if resp.status_code != 200:
                logger.error("Jira transitions fetch failed: %d", resp.status_code)
                return False

            transitions = resp.json().get("transitions", [])
            target_transition = None
            for t in transitions:
                if t["name"].lower() == target_status.lower():
                    target_transition = t
                    break
                # Partial match fallback
                if target_status.lower() in t["name"].lower():
                    target_transition = t

            if not target_transition:
                logger.warning(
                    "No transition to '%s' found for %s. Available: %s",
                    target_status, issue_key,
                    [t["name"] for t in transitions],
                )
                return False

            # Execute transition
            resp = await http.post(
                f"{_base_url()}/rest/api/3/issue/{issue_key}/transitions",
                json={"transition": {"id": target_transition["id"]}},
                auth=_auth(),
                headers=_headers(),
            )
            if resp.status_code == 204:
                logger.info("Jira %s transitioned to '%s'", issue_key, target_status)
                return True
            else:
                logger.error("Jira transition failed (%d): %s", resp.status_code, resp.text[:300])
                return False
    except httpx.RequestError as exc:
        logger.error("Jira transition error: %s", exc)
        return False


# ── High-level lifecycle methods (called from main.py pipeline) ───────────────

async def on_incident_created(incident_id: str, service: str, namespace: str,
                              message: str, severity: str) -> Optional[str]:
    """Pipeline hook: new incident detected."""
    return await create_incident_ticket(incident_id, service, namespace, message, severity)


async def on_rca_complete(incident_id: str, root_cause: str, confidence: int,
                          remediation_type: str, command: str) -> None:
    """Pipeline hook: RCA/decision engine finished."""
    comment = (
        f"🔍 Root Cause Analysis Complete\n\n"
        f"Root Cause: {root_cause}\n"
        f"Confidence: {confidence}%\n"
        f"Recommended Action: {remediation_type}\n"
        f"Command: {command or 'none'}"
    )
    await add_comment(incident_id, comment)
    await transition_ticket(incident_id, "In Progress")


async def on_remediation_executed(incident_id: str, command: str, output: str,
                                  success: bool) -> None:
    """Pipeline hook: remediation command was executed."""
    status_emoji = "✅" if success else "❌"
    comment = (
        f"{status_emoji} Remediation Executed\n\n"
        f"Command: {command}\n"
        f"Success: {success}\n"
        f"Output: {output[:500]}"
    )
    await add_comment(incident_id, comment)


async def on_incident_resolved(incident_id: str, total_time_seconds: float,
                               action_taken: str) -> None:
    """Pipeline hook: incident resolved successfully."""
    comment = (
        f"✅ Incident Resolved\n\n"
        f"Time to resolve: {total_time_seconds:.0f}s\n"
        f"Action: {action_taken}\n"
        f"Resolved autonomously by KIRA."
    )
    await add_comment(incident_id, comment)
    await transition_ticket(incident_id, "Done")


async def on_incident_failed(incident_id: str, reason: str) -> None:
    """Pipeline hook: incident could not be resolved autonomously."""
    comment = (
        f"⚠️ Autonomous Resolution Failed\n\n"
        f"Reason: {reason}\n"
        f"Manual intervention required."
    )
    await add_comment(incident_id, comment)
    # Leave ticket open / in progress for manual pickup


def get_ticket_key(incident_id: str) -> Optional[str]:
    """Get the Jira ticket key for an incident (for UI display)."""
    return _incident_to_jira.get(incident_id)


def get_ticket_url(incident_id: str) -> Optional[str]:
    """Get the full Jira ticket URL for an incident."""
    key = _incident_to_jira.get(incident_id)
    if key:
        return f"{_base_url()}/browse/{key}"
    return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _short_alert(message: str) -> str:
    """Truncate alert message for ticket summary (max 100 chars)."""
    if len(message) <= 100:
        return message
    return message[:97] + "..."
