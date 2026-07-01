"""Ask Sherlock — conversational Q&A about a specific incident.
Takes any question and answers using the full incident context as grounding."""
from __future__ import annotations

import logging

from backend.integrations.openai_client import generate

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are KIRA, an AI-powered Kubernetes SRE agent. "
    "You have fully investigated a production incident and have complete context. "
    "Answer questions concisely and accurately, citing specific evidence from the incident where relevant. "
    "Be direct and helpful. Do not repeat the full context unless explicitly asked. "
    "If asked about something not covered by the incident data, say so clearly."
)


async def answer_question(incident: object, question: str) -> str:
    """Answer a question about an incident using its full context."""
    rca = getattr(incident, "rca", None)
    remediation = getattr(incident, "remediation", None)
    agent_opinions = getattr(incident, "agent_opinions", [])
    timeline = getattr(incident, "timeline", [])

    agent_opinions_text = ""
    if agent_opinions:
        agent_opinions_text = "\n\nSPECIALIST AGENT FINDINGS:\n" + "\n".join(
            f"  [{op.agent}] ({op.confidence}% confidence): {op.finding}"
            + (f"\n    Recommendation: {op.recommendation}" if op.recommendation else "")
            for op in agent_opinions
        )

    timeline_summary = "\n".join(
        f"  [{ev.step}] {ev.detail[:120]}"
        for ev in timeline[-15:]
    )

    context = f"""INCIDENT CONTEXT:
Service: {incident.alert.service}
Namespace: {incident.alert.namespace}
Alert: {incident.alert.message}
Status: {incident.status}
Started: {incident.started_at}
Resolved: {getattr(incident, 'resolved_at', None) or 'not yet'}
Time to resolve: {f"{incident.total_time_seconds:.0f}s" if incident.total_time_seconds else "ongoing"}

ROOT CAUSE: {rca.root_cause if rca else 'Not yet determined'}
CONFIDENCE: {rca.confidence if rca else 'N/A'}%
REMEDIATION TYPE: {incident.remediation.action if remediation else 'none taken'}
BLAST RADIUS: {rca.blast_radius if rca else 'unknown'}{agent_opinions_text}

RECENT TIMELINE:
{timeline_summary}

QUESTION: {question}"""

    try:
        return await generate(context, system=_SYSTEM)
    except Exception as exc:
        logger.exception("Chat answer failed for question: %s", question)
        return f"Sorry, I encountered an error processing your question: {exc}"
