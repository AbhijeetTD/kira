"""Auto-Postmortem Generator — produces a structured markdown incident postmortem
from a resolved Incident object using Azure OpenAI GPT-5.4."""
from __future__ import annotations

import logging

from backend.integrations.openai_client import generate

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are an experienced SRE technical writer. "
    "Generate clear, actionable incident postmortems in clean markdown. "
    "Be concise but thorough. Cite specific evidence. "
    "Use blameless language - focus on systems and processes, not individuals. "
    "Respond with clean markdown only - no extra commentary. "
    "IMPORTANT: Do NOT use emojis or special unicode characters anywhere in the output. "
    "Use plain ASCII text only. Use backticks for code/commands. "
    "Do NOT wrap the response in markdown code fences like ```markdown."
)

_TEMPLATE = """\
Generate a complete incident postmortem for the following Kubernetes incident.

SERVICE: {service}
NAMESPACE: {namespace}
ALERT: {message}
STARTED: {started_at}
RESOLVED: {resolved_at}
TIME TO RESOLVE: {ttr}
FINAL STATUS: {status}

ROOT CAUSE ANALYSIS:
{root_cause}

SPECIALIST AGENT FINDINGS:
{agent_findings}

REMEDIATION EXECUTED:
{remediation}

INCIDENT TIMELINE (chronological):
{timeline}

Write the postmortem using EXACTLY this markdown structure (fill in all sections):

# Incident Postmortem - {service} / {started_at}

## Executive Summary
[2-3 sentences: what happened, business impact, and how it was resolved. Write for a non-technical executive audience.]

## Incident Details
| Field | Value |
|---|---|
| Service | {service} |
| Namespace | {namespace} |
| Severity | critical |
| Time to Resolve | {ttr} |
| Status | {status} |
| Detection Source | [alert source from timeline] |

## Root Cause
[One clear paragraph: the primary root cause with specific evidence cited]

### Contributing Factors
[Bullet list of secondary issues that worsened the incident]

### Blast Radius
[What services or users were affected and for how long]

## Detection and Response
[How was the incident detected? What triggered the alert? How quickly was it picked up?]

## AI Agent Analysis
[Summarise what each specialist agent found and how the judge synthesised their findings]

## Remediation Actions
[Specific actions taken: what command ran, what changed, what was rolled back]

## Impact Assessment
[Estimated number of affected users, services down, error rate during incident]

## Prevention Recommendations
[3-5 concrete, actionable recommendations to prevent recurrence. Be specific.]

## Lessons Learned
[2-3 key takeaways for the team - blameless, process-focused]

## Action Items
| Action | Owner | Priority | Due |
|---|---|---|---|
| [specific action 1] | [team] | High | [timeframe] |
| [specific action 2] | [team] | Medium | [timeframe] |
| [specific action 3] | [team] | Low | [timeframe] |
"""


async def generate_postmortem(incident: object) -> str:
    """Generate a markdown postmortem from a resolved Incident. Caches result on incident."""
    rca = getattr(incident, "rca", None)
    remediation = getattr(incident, "remediation", None)
    agent_opinions = getattr(incident, "agent_opinions", [])
    timeline = getattr(incident, "timeline", [])

    root_cause_text = "Root cause not yet analysed."
    if rca:
        points = "\n".join(f"  - {p}" for p in (rca.root_cause_points or []))
        root_cause_text = f"{rca.root_cause}\n\nKey findings:\n{points}"
        if rca.contributing_factors:
            root_cause_text += "\n\nContributing factors:\n" + "\n".join(
                f"  - {f}" for f in rca.contributing_factors
            )
        if rca.blast_radius and rca.blast_radius.lower() != "none":
            root_cause_text += f"\n\nBlast radius: {rca.blast_radius}"

    agent_findings_text = "Specialist agents did not run (single-agent mode)."
    if agent_opinions:
        agent_findings_text = "\n".join(
            f"- [{op.agent}] ({op.confidence}% confidence): {op.finding}"
            + (f"\n  Recommendation: {op.recommendation}" if op.recommendation else "")
            for op in agent_opinions
        )

    remediation_text = "No automated remediation was executed."
    if remediation:
        remediation_text = f"Action: {remediation.action}\nCommand: {remediation.command}"
        if remediation.executed:
            remediation_text += f"\nResult: {'Success ✅' if remediation.success else 'Failed ❌'}"
            if remediation.output:
                remediation_text += f"\nOutput: {remediation.output[:400]}"

    timeline_text = "\n".join(
        f"[{ev.timestamp.strftime('%H:%M:%S')}] [{ev.step}] {ev.detail[:100]}"
        for ev in timeline[:25]
    )

    ttr = (
        f"{incident.total_time_seconds:.0f}s ({incident.total_time_seconds / 60:.1f} min)"
        if incident.total_time_seconds
        else "ongoing"
    )
    started_str = incident.started_at.strftime("%Y-%m-%d %H:%M UTC")
    resolved_str = (
        incident.resolved_at.strftime("%Y-%m-%d %H:%M UTC")
        if incident.resolved_at
        else "ongoing"
    )

    prompt = _TEMPLATE.format(
        service=incident.alert.service,
        namespace=incident.alert.namespace,
        message=incident.alert.message,
        started_at=started_str,
        resolved_at=resolved_str,
        ttr=ttr,
        status=incident.status,
        root_cause=root_cause_text,
        agent_findings=agent_findings_text,
        remediation=remediation_text,
        timeline=timeline_text,
    )

    try:
        return await generate(prompt, system=_SYSTEM)
    except Exception as exc:
        logger.exception("Postmortem generation failed for incident %s", incident.id)
        return (
            f"# Postmortem — {incident.alert.service}\n\n"
            f"**Generation failed:** {exc}\n\n"
            "Manual postmortem required. See incident timeline for details."
        )
