"""Multi-Agent War Room — 4 specialist AI agents analyse cluster evidence in parallel
then a Judge agent synthesises their findings into a final RCA verdict.

Pipeline:
  SRE Agent   ──┐
  App Agent   ──┼──► Judge Agent ──► RCAResult
  Sec Agent   ──┘
  Cost Agent  ──┘
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import List

from backend.integrations.openai_client import generate
from backend.models.incident import AgentOpinion, Evidence, TimelineEvent

logger = logging.getLogger(__name__)

# ── Specialist agent definitions ─────────────────────────────────────────────

SPECIALIST_AGENTS: list[dict] = [
    {
        "name": "SRE Agent",
        "icon": "🔧",
        "color": "blue",
        "system": (
            "You are a Senior Site Reliability Engineer specialist in Kubernetes infrastructure.\n"
            "YOUR LANE — ONLY analyse these topics:\n"
            "  • Pod lifecycle: phases (Pending/Running/CrashLoopBackOff/OOMKilled/Evicted), restart counts\n"
            "  • Deployment rollout: desired vs available vs ready replicas, stalled rollouts\n"
            "  • Node scheduling: FailedScheduling events, node pressure (CPU/memory/disk), taints/tolerations\n"
            "  • Probes: readiness/liveness probe failures visible in events\n"
            "  • Volume issues: mount failures, PVC binding errors\n\n"
            "STAY OUT OF:\n"
            "  • Application-level logs, HTTP errors, stack traces (that's the App Agent's job)\n"
            "  • Image security, RBAC, secrets (that's the Security Agent's job)\n"
            "  • Cost optimisation, right-sizing recommendations (that's the Cost Agent's job)\n\n"
            "CRITICAL: Use the DEPLOYMENT DESCRIBE section for CURRENT state. "
            "Rollout history shows PAST deployments — do NOT cite old annotations as current problems.\n"
            "If pods are Running, Ready, 0 restarts, and no scheduling issues exist, "
            "say 'infrastructure is healthy' with confidence <= 30.\n"
            "Respond with valid JSON only — no markdown, no extra text."
        ),
        "focus": "pod lifecycle, restart counts, scheduling events, rollout status, node pressure",
    },
    {
        "name": "App Agent",
        "icon": "📱",
        "color": "green",
        "system": (
            "You are a Senior Application Engineer specialist in debugging distributed systems.\n"
            "YOUR LANE — ONLY analyse these topics:\n"
            "  • Application logs: error messages, exceptions, stack traces, HTTP 4xx/5xx codes\n"
            "  • Startup failures: container entrypoint errors, missing config files, bad commands\n"
            "  • Dependency errors: database connection refused, cache timeouts, DNS resolution failures\n"
            "  • Configuration: environment variable mismatches, missing config maps, wrong ports\n\n"
            "STAY OUT OF:\n"
            "  • Infrastructure metrics (CPU/memory limits, scheduling) — that's the SRE Agent's job\n"
            "  • Image security, RBAC — that's the Security Agent's job\n"
            "  • Cost and resource sizing — that's the Cost Agent's job\n\n"
            "CRITICAL: Focus on the POD LOGS section. If logs only show healthy requests "
            "(200 status codes, probe checks), say 'application is functioning normally' "
            "with confidence <= 30.\n"
            "Do NOT repeat infrastructure findings like 'insufficient CPU' or 'scheduling pressure'.\n"
            "Respond with valid JSON only — no markdown, no extra text."
        ),
        "focus": "application logs, error messages, startup failures, dependency connectivity",
    },
    {
        "name": "Security Agent",
        "icon": "🔒",
        "color": "amber",
        "system": (
            "You are a Kubernetes Security Specialist.\n"
            "YOUR LANE — ONLY analyse these topics:\n"
            "  • Image security: unversioned tags (:latest), images from untrusted registries, "
            "recent image changes that may introduce vulnerabilities\n"
            "  • RBAC / ServiceAccount: missing permissions, overly permissive roles\n"
            "  • Secrets exposure: secrets mounted as env vars, config maps with sensitive data\n"
            "  • Network: pods without network policies, unexpected port exposure\n"
            "  • Privilege escalation: privileged containers, hostPath mounts, runAsRoot\n\n"
            "STAY OUT OF:\n"
            "  • Pod lifecycle, restarts, scheduling — that's the SRE Agent's job\n"
            "  • Application logs and errors — that's the App Agent's job\n"
            "  • Resource limits and cost — that's the Cost Agent's job\n\n"
            "CRITICAL: Use the DEPLOYMENT DESCRIBE section for the CURRENT image and config. "
            "If the incident is purely operational (scheduling, restarts, resource sizing) with "
            "no security angle, say 'no security concerns identified' with confidence <= 20.\n"
            "Do NOT restate infrastructure or application findings.\n"
            "Respond with valid JSON only — no markdown, no extra text."
        ),
        "focus": "image provenance, RBAC, secrets exposure, network policies, privilege escalation",
    },
    {
        "name": "Cost Agent",
        "icon": "💰",
        "color": "purple",
        "system": (
            "You are a Cloud FinOps and Kubernetes Resource Optimisation Specialist.\n"
            "YOUR LANE — ONLY analyse these topics:\n"
            "  • Resource efficiency: compare ACTUAL usage (from RESOURCE USAGE) vs configured "
            "limits (from DEPLOYMENT DESCRIBE) — are resources over- or under-provisioned?\n"
            "  • Request/limit ratio: are requests and limits appropriately balanced?\n"
            "  • Replica count efficiency: are there more replicas than needed for the workload?\n"
            "  • Right-sizing: recommend specific limit/request values based on ACTUAL usage data\n"
            "  • Waste detection: idle replicas, oversized limits relative to actual consumption\n\n"
            "STAY OUT OF:\n"
            "  • Pod lifecycle, scheduling events — that's the SRE Agent's job\n"
            "  • Application logs and errors — that's the App Agent's job\n"
            "  • Image security, RBAC — that's the Security Agent's job\n\n"
            "CRITICAL: Read DEPLOYMENT DESCRIBE for current limits and RESOURCE USAGE for actual consumption.\n"
            "Base your analysis on the NUMBERS: actual CPU (millicores) vs limit, actual memory vs limit.\n"
            "If actual usage data is not available, say so — do NOT invent usage numbers.\n"
            "If limits are adequate and pods are healthy, say 'resources are appropriately sized' "
            "with confidence <= 30.\n"
            "Do NOT repeat infrastructure or scheduling findings. Your job is cost and efficiency ONLY.\n"
            "Respond with valid JSON only — no markdown, no extra text."
        ),
        "focus": "actual vs configured resource usage, request/limit ratio, replica efficiency, right-sizing",
    },
]

_SPECIALIST_PROMPT = """\
You are investigating a Kubernetes production incident.

SERVICE: {service}  |  NAMESPACE: {namespace}
ALERT: {message}

=== CURRENT STATE (this is the ACTUAL, LIVE configuration — trust these values) ===

--- DEPLOYMENT DESCRIBE (current config) ---
{deployment_describe}

--- POD STATUS (live) ---
{pod_status}

--- RESOURCE USAGE (live metrics) ---
{resource_usage}

--- RECENT EVENTS (warnings from the cluster) ---
{recent_events}

--- POD LOGS (recent output) ---
{pod_logs}

=== HISTORICAL DATA (past deployments — NOT the current state) ===

--- ROLLOUT HISTORY ---
{rollout_history}

CRITICAL RULES:
  - The DEPLOYMENT DESCRIBE section shows the CURRENT resource limits, image, and replica count.
  - The ROLLOUT HISTORY shows PAST deployments and their change-cause annotations.
    Do NOT cite rollout history annotations as the current problem.
    For example, if rollout history says "FAULT: 4Mi memory" but deployment describe
    shows memory=256Mi, the CURRENT memory IS 256Mi — the 4Mi was a past issue.
  - Base your analysis on CURRENT STATE sections. Only use rollout history to understand
    whether a recent change may have caused the current problem.
  - If all pods are Running, Ready, 0 restarts, and current resource limits are adequate,
    report "no issues found" with low confidence.
  - NEVER invent or assume problems that are NOT explicitly visible in the evidence.
    If pod status shows Restarts:0, do NOT say "elevated restart count".
    If there is no OOMKilled in pod status, do NOT say "OOMKilled events".
    If there is no CrashLoopBackOff, do NOT mention CrashLoopBackOff.
  - Your finding MUST be provable from the evidence text shown above.
    Before writing each claim, check: can I point to a specific line in the evidence?
    If not, do NOT make that claim.

Your domain focus: {focus}

Respond ONLY with this exact JSON (no extra text):
{{
  "finding": "<detailed 3-5 sentence analysis of what you found. Cite CURRENT pod names, metrics, error messages, and resource values from the DEPLOYMENT DESCRIBE section. Explain the causal chain — what went wrong and why. If nothing is wrong, say so.>",
  "evidence_cited": ["<specific CURRENT evidence: pod name, event message, or metric from live data>"],
  "confidence": <integer 0-100, lower if no clear problem exists>,
  "recommendation": "<2-3 sentences: the specific action you would take from your domain perspective, including exact values or commands where applicable. Say 'no action needed' if the deployment is healthy.>",
  "concerns": ["<concern 1>", "<concern 2>"]
}}"""

# ── JSON helpers ──────────────────────────────────────────────────────────────

def _repair_truncated_json(text: str) -> str | None:
    """Attempt to close a JSON object that was truncated mid-output."""
    repaired = text.rstrip()
    repaired = re.sub(r',\s*"[^"]*"\s*:\s*"[^"]*$', '', repaired)
    repaired = re.sub(r',\s*"[^"]*"\s*:\s*\[$', '', repaired)
    repaired = re.sub(r',\s*"[^"]*"\s*:\s*\[\s*"[^"]*$', '', repaired)
    repaired = re.sub(r',\s*"[^"]*"\s*$', '', repaired)
    repaired = re.sub(r',\s*"[^"]*$', '', repaired)
    repaired = re.sub(r',\s*\{[^}]*$', '', repaired)

    open_braces = repaired.count('{') - repaired.count('}')
    open_brackets = repaired.count('[') - repaired.count(']')

    in_string = False
    escaped = False
    for ch in repaired:
        if escaped:
            escaped = False
            continue
        if ch == '\\':
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
    if in_string:
        repaired += '"'

    repaired += ']' * max(0, open_brackets)
    repaired += '}' * max(0, open_braces)

    if open_braces > 0 or open_brackets > 0:
        return repaired
    return None


def _extract_json(text: str) -> dict:  # noqa: C901
    """Parse JSON from LLM output with multiple fallback strategies."""
    text = text.strip()

    # Strategy 1: direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strategy 2: find the outermost {...} block and parse it
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Strategy 3: repair JSON where LLM appended text OUTSIDE a closing string quote.
    # Pattern: "...value" some text,   →  "...value some text",
    # Also handles: "...value" from Agent Name  inside arrays.
    if match:
        block = match.group()
        # Move trailing " text before the next comma/bracket back inside the string
        repaired = re.sub(
            r'"([^"\\]*(?:\\.[^"\\]*)*)"\s+([^,\]\}"]+?)\s*([,\]\}])',
            lambda m: f'"{m.group(1)} {m.group(2).strip()}"{m.group(3)}',
            block,
        )
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass

    # Strategy 4: collapse adjacent "" (empty string artifacts)
    if match:
        cleaned = re.sub(r'""', "'", match.group())
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

    # Strategy 5: attempt to repair truncated JSON by closing open strings/arrays/objects
    if match:
        block = match.group()
        repaired = _repair_truncated_json(block)
        if repaired:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass

    # Strategy 6: regex-extract individual scalar fields — works even when the
    # surrounding JSON is broken by badly escaped evidence_cited entries.
    result: dict = {}
    for field, pattern in [
        ("finding",        r'"finding"\s*:\s*"((?:[^"\\]|\\.)*)"'),
        ("confidence",     r'"confidence"\s*:\s*(\d+)'),
        ("recommendation", r'"recommendation"\s*:\s*"((?:[^"\\]|\\.)*)"'),
        ("root_cause_summary", r'"root_cause_summary"\s*:\s*"((?:[^"\\]|\\.)*)"'),
        ("remediation_type", r'"remediation_type"\s*:\s*"((?:[^"\\]|\\.)*)"'),
        ("remediation_command", r'"remediation_command"\s*:\s*"((?:[^"\\]|\\.)*)"'),
        ("remediation_reason", r'"remediation_reason"\s*:\s*"((?:[^"\\]|\\.)*)"'),
    ]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            result[field] = int(m.group(1)) if field == "confidence" else m.group(1)

    # If we got judge-level fields, fill in defaults
    if "root_cause_summary" in result:
        result.setdefault("confidence", 50)
        result.setdefault("root_cause_points", [])
        result.setdefault("contributing_factors", [])
        result.setdefault("blast_radius", "unknown")
        result.setdefault("affected_services", [])
        result.setdefault("remediation_type", "none")
        result.setdefault("remediation_reason", "")
        result.setdefault("remediation_command", "")
        return result

    if "finding" in result:
        result.setdefault("confidence", 50)
        result.setdefault("evidence_cited", [])
        result.setdefault("recommendation", "")
        result.setdefault("concerns", [])
        return result

    raise ValueError(f"No valid JSON found in LLM response: {text[:300]}")


async def _run_specialist(
    agent_cfg: dict,
    service: str,
    namespace: str,
    message: str,
    evidence: Evidence,
    queue: object,
) -> AgentOpinion:
    name = agent_cfg["name"]
    icon = agent_cfg["icon"]

    await queue.put(TimelineEvent(
        step=f"War Room — {name}",
        status="running",
        detail=f"{name} is analysing cluster evidence from its domain…",
    ))

    try:
        def _safe(s: str | None, limit: int) -> str:
            """Truncate and replace double-quotes so the LLM can safely embed
            the text inside a JSON string without breaking its own output.
            Error-state lines are prioritised so they aren't lost to truncation."""
            text = (s or "N/A").replace('"', "'")
            if len(text) <= limit:
                return text
            # Prioritise error lines so truncation doesn't hide failing pods
            error_keywords = ("error", "crashloop", "oomkill", "imagepull",
                              "pending", "backoff", "failed", "warning",
                              "createcontainer", "evicted", "0/")
            lines = text.splitlines()
            error_lines = [l for l in lines if any(k in l.lower() for k in error_keywords)]
            normal_lines = [l for l in lines if not any(k in l.lower() for k in error_keywords)]
            # Rebuild: error lines first, then normal lines, within budget
            reordered = "\n".join(error_lines + normal_lines)
            return reordered[:limit]

        prompt = _SPECIALIST_PROMPT.format(
            service=service,
            namespace=namespace,
            message=message,
            deployment_describe=_safe(evidence.deployment_describe, 800),
            pod_status=_safe(evidence.pod_status, 1500),
            pod_logs=_safe(evidence.pod_logs, 2000),
            resource_usage=_safe(evidence.resource_usage, 500),
            rollout_history=_safe(evidence.rollout_history, 800),
            recent_events=_safe(evidence.recent_events, 1000),
            focus=agent_cfg["focus"],
        )
        raw = await asyncio.wait_for(
            generate(prompt, system=agent_cfg["system"]),
            timeout=90.0,
        )
        data = _extract_json(raw)

        opinion = AgentOpinion(
            agent=name,
            icon=icon,
            color=agent_cfg["color"],
            finding=data.get("finding", "No finding reported."),
            evidence_cited=data.get("evidence_cited", []),
            confidence=int(data.get("confidence", 50)),
            recommendation=data.get("recommendation", ""),
            concerns=data.get("concerns", []),
        )

        await queue.put(TimelineEvent(
            step=f"War Room — {name}",
            status="success",
            detail=f"{opinion.finding}  |  Confidence: {opinion.confidence}%",
        ))
        return opinion

    except Exception as exc:
        logger.exception("War room specialist %s failed", name)
        fallback = AgentOpinion(
            agent=name,
            icon=icon,
            color=agent_cfg["color"],
            finding=f"Analysis failed: {exc}",
            evidence_cited=[],
            confidence=0,
            recommendation="Unable to provide recommendation.",
            concerns=[str(exc)],
        )
        await queue.put(TimelineEvent(
            step=f"War Room — {name}",
            status="error",
            detail=f"{name} analysis failed: {exc}",
        ))
        return fallback


async def run_war_room(
    service: str,
    namespace: str,
    message: str,
    evidence: Evidence,
    queue: object,
) -> List[AgentOpinion]:
    """Run 4 specialist agents sequentially. Returns opinions only.

    The Judge/decision-making is now handled by the GPT Decision Engine
    (decision_engine.py), which takes these opinions + raw evidence and
    produces the final RCA + remediation in one GPT call.
    """

    await queue.put(TimelineEvent(
        step="War Room",
        status="info",
        detail="Multi-Agent War Room launched — 4 specialist agents will investigate in sequence.",
    ))

    # Run specialists sequentially (Azure OpenAI handles concurrent requests)
    opinions: List[AgentOpinion] = []
    for agent_cfg in SPECIALIST_AGENTS:
        opinion = await _run_specialist(agent_cfg, service, namespace, message, evidence, queue)
        opinions.append(opinion)

    await queue.put(TimelineEvent(
        step="War Room",
        status="success",
        detail=f"All {len(opinions)} specialist agents completed — forwarding to GPT Decision Engine.",
    ))

    return opinions
