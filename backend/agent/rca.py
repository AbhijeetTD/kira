"""Root Cause Analysis — sends cluster evidence to Azure OpenAI and parses a
structured JSON response containing root cause, confidence score, and
recommended remediation action."""
from __future__ import annotations

import json
import logging
import re

from backend.integrations.openai_client import generate
from backend.models.incident import Evidence, RCAResult, RemediationType

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are an expert Kubernetes SRE with deep knowledge of production incident "
    "response. You analyze cluster evidence and identify the precise root cause of "
    "incidents. Always respond with valid JSON only — no markdown code fences, no "
    "extra text, no explanation outside the JSON object.\n\n"
    "ANALYSIS GUIDELINES:\n"
    "- root_cause_summary: one clear title line (e.g. \"Bad deployment v2.1 caused CrashLoopBackOff\")\n"
    "- root_cause_points: 3-5 bullet findings, each citing SPECIFIC evidence from the data "
    "(pod names, log lines, event messages, image names, revision numbers)\n"
    "- contributing_factors: secondary issues that worsened the incident\n"
    "- blast_radius: downstream services/users affected; \"none\" if isolated\n\n"
    "REMEDIATION DECISION RULES (apply in priority order):\n"
    "1. CrashLoopBackOff OR high restart count AND rollout history shows a change in "
    "   the last hour → remediation_type = 'rollback'\n"
    "2. Pods Running but CPU throttled OR OOMKilled AND recent deployment reduced "
    "   resource limits → remediation_type = 'rollback'\n"
    "3. Connection pool exhaustion OR config mismatch introduced by recent deployment "
    "   → remediation_type = 'rollback'\n"
    "4. Pods Running but health checks failing AND no recent deployment change "
    "   → remediation_type = 'restart'\n"
    "5. All pods healthy but insufficient replica count for current load "
    "   → remediation_type = 'scale'\n"
    "6. Resource limits need tuning but no deployable rollback available "
    "   → remediation_type = 'patch'\n"
    "7. No clear infrastructure cause found → remediation_type = 'none'"
)

_PROMPT_TEMPLATE = """\
Analyze the following evidence from a Kubernetes production incident.

SERVICE: {service}
NAMESPACE: {namespace}

--- POD STATUS ---
{pod_status}

--- POD LOGS (last 80 lines) ---
{pod_logs}

--- RESOURCE USAGE (kubectl top pods) ---
{resource_usage}

--- ROLLOUT HISTORY ---
{rollout_history}

--- DEPLOYMENT DESCRIBE ---
{deployment_describe}

--- RECENT EVENTS ---
{recent_events}

--- CORRELATED FAILING SERVICES (downstream impact) ---
{correlated_services}

Based on this evidence, respond ONLY with a JSON object in this exact format:
{{
  "root_cause_summary": "<1-line clear title of the root cause>",
  "root_cause_points": [
    "<Finding 1: specific observation + what it means, cite evidence>",
    "<Finding 2: specific observation + what it means, cite evidence>",
    "<Finding 3: specific observation + what it means, cite evidence>"
  ],
  "contributing_factors": ["<secondary factor 1>", "<secondary factor 2>"],
  "blast_radius": "<downstream services or users impacted, or 'none' if isolated>",
  "affected_services": ["<service1>"],
  "confidence": <integer 0-100>,
  "remediation_type": "<one of: rollback, restart, scale, patch, none>",
  "remediation_reason": "<one sentence: why this specific action fixes the root cause>"
}}

Requirements:
- root_cause_points must cite SPECIFIC evidence (pod names, log lines, image tags, revision IDs)
- contributing_factors are secondary issues, not the primary cause
- confidence = 0 only if evidence is completely missing or contradictory
- Every field is required; use empty list [] if nothing applicable"""


def _extract_json(text: str) -> dict:
    """Try direct parse then fall back to regex extraction."""
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise ValueError("No valid JSON object found in LLM response")


async def analyze(
    service: str,
    namespace: str,
    evidence: Evidence,
) -> RCAResult:
    prompt = _PROMPT_TEMPLATE.format(
        service=service,
        namespace=namespace,
        pod_status=evidence.pod_status or "N/A",
        pod_logs=evidence.pod_logs or "N/A",
        resource_usage=evidence.resource_usage or "N/A",
        rollout_history=evidence.rollout_history or "N/A",
        deployment_describe=evidence.deployment_describe or "N/A",
        recent_events=evidence.recent_events or "N/A",
        correlated_services=evidence.correlated_services or "N/A",
    )

    response = await generate(prompt, system=_SYSTEM_PROMPT)
    logger.debug("LLM raw response: %s", response[:500])

    try:
        data = _extract_json(response)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Failed to parse LLM JSON (%s). Using safe fallback.", exc)
        return RCAResult(
            root_cause=(
                "AI analysis could not determine root cause automatically. "
                "Manual investigation required."
            ),
            affected_services=[service],
            confidence=0,
            remediation_type=RemediationType.NONE,
            remediation_reason="LLM response could not be parsed.",
        )

    raw_type = str(data.get("remediation_type", "none")).lower()
    try:
        remediation_type = RemediationType(raw_type)
    except ValueError:
        remediation_type = RemediationType.NONE

    return RCAResult(
        root_cause=str(data.get("root_cause_summary") or data.get("root_cause", "Unknown root cause")),
        root_cause_points=[str(p) for p in data.get("root_cause_points", [])],
        contributing_factors=[str(f) for f in data.get("contributing_factors", [])],
        blast_radius=str(data.get("blast_radius", "")) or None,
        affected_services=list(data.get("affected_services", [service])),
        confidence=int(data.get("confidence", 0)),
        remediation_type=remediation_type,
        remediation_reason=str(data.get("remediation_reason", "")),
    )
