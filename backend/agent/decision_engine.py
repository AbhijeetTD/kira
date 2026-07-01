"""GPT-powered Decision Engine — replaces hardcoded pattern matching with LLM reasoning.

Instead of brittle regex patterns for known failure modes, this engine uses GPT to:
1. Analyse all cluster evidence + specialist agent opinions
2. Determine the root cause and best remediation
3. Generate the exact kubectl command with real values from the evidence
4. Assign a confidence score based on evidence quality and agent agreement

This is the SOLE decision-maker — it replaces both the old deterministic engine
AND the War Room Judge, producing the final RCA + remediation in one GPT call.

Confidence controls approval flow:
  - confidence > auto_approve_threshold  → auto-execute
  - confidence ≤ auto_approve_threshold  → require human approval via Teams
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from backend.integrations.openai_client import generate
from backend.models.incident import (
    AgentOpinion,
    Evidence,
    RCAResult,
    RemediationType,
    TimelineEvent,
)

logger = logging.getLogger(__name__)


# ── Decision result (kept for backward compat with main.py) ──────────────────

@dataclass
class Decision:
    """Result of the GPT decision engine."""
    remediation_type: RemediationType
    confidence: int                  # 0-100
    reason: str                      # human-readable explanation
    pattern: str = "gpt_engine"      # always "gpt_engine" now
    command: str = ""                # exact kubectl command to execute
    root_cause: str = ""
    root_cause_points: list = field(default_factory=list)
    contributing_factors: list = field(default_factory=list)
    blast_radius: str = ""
    affected_services: list = field(default_factory=list)


# ── Utility helpers (used by main.py retry logic) ────────────────────────────

def _kube_context_flag() -> str:
    from backend.config import settings
    return f" --context {settings.kube_context}" if settings.kube_context else ""


def _get_deployment_info(evidence: Evidence) -> dict:
    """Extract deployment info from structured data or text fallback.
    Includes ALL containers (main + sidecars) — not just containers[0].
    """
    if evidence.deployment_info:
        di = evidence.deployment_info
        info: dict = {
            "deployment": di.get("deployment", ""),
            "kind": di.get("kind", "deployment"),
            "desired": di.get("desired"),
            "available": di.get("available"),
            "ready": di.get("ready"),
            "updated": di.get("updated"),
        }
        containers = di.get("containers", [])
        if containers:
            # Primary container (first one — used for commands)
            c = containers[0]
            info["container"] = c.get("name", info["deployment"])
            limits = c.get("limits", {})
            requests = c.get("requests", {})
            if limits.get("cpu"):
                info["cpu_limit"] = limits["cpu"]
            if limits.get("memory"):
                info["mem_limit"] = limits["memory"]
            if requests.get("cpu"):
                info["cpu_request"] = requests["cpu"]
            if requests.get("memory"):
                info["mem_request"] = requests["memory"]
            # ALL containers (for multi-container awareness)
            if len(containers) > 1:
                info["all_containers"] = [
                    {
                        "name": ct.get("name", ""),
                        "image": ct.get("image", ""),
                        "limits": ct.get("limits", {}),
                        "requests": ct.get("requests", {}),
                    }
                    for ct in containers
                ]
        return info

    describe = evidence.deployment_describe or ""
    info = {}
    m = re.search(r"(?:Deployment|StatefulSet):\s*(\S+)", describe)
    info["deployment"] = m.group(1) if m else ""
    if "StatefulSet:" in describe:
        info["kind"] = "statefulset"
    else:
        info["kind"] = "deployment"
    m = re.search(r"Container:\s*(\S+)", describe)
    info["container"] = m.group(1) if m else info["deployment"]
    return info


# ── Pre-analysis helpers (run BEFORE GPT to enrich context) ──────────────────

def _compute_health_summary(evidence: Evidence) -> dict:
    """Pre-compute a structured health check from evidence so GPT doesn't
    have to parse raw text for basic health signals."""
    pod_status = evidence.pod_status or ""
    deploy_desc = evidence.deployment_describe or ""
    events = evidence.recent_events or ""
    resource_usage = evidence.resource_usage or ""

    summary: dict = {
        "all_running": False,
        "zero_restarts": False,
        "all_ready": False,
        "crash_loop": False,
        "oom_killed": False,
        "image_pull_error": False,
        "pending_pods": False,
        "init_container_failure": False,
        "node_pressure": False,
        "config_error": False,
        "terminating_pods": False,
        "evicted_pods": False,
        "hpa_active": False,
        "pvc_issues": False,
    }

    combined = (pod_status + " " + events).lower()

    # Check for non-running pods (ContainerCreating, CrashLoopBackOff, Error, etc.)
    pod_status_lower = pod_status.lower()
    has_non_running_pods = any(
        state in pod_status_lower
        for state in ("containercreating", "crashloopbackoff", "error", "pending",
                      "imagepullbackoff", "errimagepull", "terminated", "init:")
    )
    # Count ready vs not-ready from pod status lines (e.g. "1/1 Ready" vs "0/1 Ready")
    not_ready_pods = len(re.findall(r"0/\d+\s+ready", pod_status_lower))
    # Detect partially ready pods (e.g. "3/4 Ready" — stuck rollout / probe failure)
    partially_ready = [
        m.group(0) for m in re.finditer(r'(\d+)/(\d+)\s+Ready', pod_status)
        if m.group(1) != m.group(2) and m.group(1) != "0"
    ]

    summary["all_running"] = (
        "running" in pod_status_lower
        and not has_non_running_pods
        and not_ready_pods == 0
        and len(partially_ready) == 0
    )
    summary["partially_ready_pods"] = len(partially_ready) > 0
    summary["all_ready"] = not_ready_pods == 0 and len(partially_ready) == 0
    summary["zero_restarts"] = "restarts:0" in pod_status.replace(" ", "").lower() or "0         " in pod_status
    summary["crash_loop"] = "crashloopbackoff" in combined
    summary["oom_killed"] = "oomkilled" in combined or "oom-killed" in combined or "oom_killed" in combined
    summary["image_pull_error"] = "imagepullbackoff" in combined or "errimagepull" in combined
    summary["pending_pods"] = "pending" in pod_status_lower
    summary["container_creating"] = "containercreating" in pod_status_lower
    summary["init_container_failure"] = "init:" in pod_status_lower or "initcontainer" in combined
    summary["node_pressure"] = any(
        k in combined for k in ("diskpressure", "memorypressure", "pidpressure", "nodenotready", "failedscheduling")
    )
    summary["config_error"] = any(
        k in combined for k in ("configmap", "secret", "createcontainerconfigerror", "mountfailed")
    )
    summary["terminating_pods"] = "terminating" in pod_status_lower
    summary["evicted_pods"] = "evicted" in combined
    summary["hpa_active"] = "horizontalpodautoscaler" in combined or "hpa" in deploy_desc.lower()
    summary["pvc_issues"] = any(
        k in combined for k in ("persistentvolumeclaim", "pvc", "unbound", "volumemount")
    )
    # Check for sandbox/runtime failures in events
    summary["sandbox_failure"] = any(
        k in combined for k in ("failedcreatepodsandbox", "failedcreatepodsand", "runc create failed")
    )
    summary["warning_events"] = "[warning]" in events.lower() or "warning" in events.lower().split("\n")[0] if events else False

    # Replica readiness — also check raw pod status for not-ready pods
    desired_m = re.search(r"desired[=:\s]*(\d+)", deploy_desc, re.IGNORECASE)
    ready_m = re.search(r"ready[=:\s]*(\d+)", deploy_desc, re.IGNORECASE)
    if desired_m and ready_m:
        summary["desired_replicas"] = int(desired_m.group(1))
        summary["ready_replicas"] = int(ready_m.group(1))
        # all_ready requires BOTH deployment-level readiness AND no not-ready pods in pod status
        summary["all_ready"] = (
            desired_m.group(1) == ready_m.group(1)
            and not_ready_pods == 0
            and not has_non_running_pods
        )
    else:
        summary["all_ready"] = not_ready_pods == 0 and not has_non_running_pods

    # Resource limits from describe
    cpu_lim_m = re.search(r"cpu['\"]?:\s*['\"]?(\d+m?)", deploy_desc)
    mem_lim_m = re.search(r"memory['\"]?:\s*['\"]?(\d+\w+)", deploy_desc)
    if cpu_lim_m:
        summary["current_cpu_limit"] = cpu_lim_m.group(1)
    if mem_lim_m:
        summary["current_mem_limit"] = mem_lim_m.group(1)

    return summary


def _parse_rollout_history(evidence: Evidence) -> str:
    """Pre-parse rollout history into a structured summary so GPT can easily
    identify the last healthy revision for --to-revision targeting."""
    history = evidence.rollout_history or ""
    if not history.strip() or history.strip() == "N/A":
        return "No rollout history available."

    lines = history.strip().splitlines()
    revisions = []
    current_rev = None

    for line in lines:
        rev_m = re.match(r"(?:REVISION|revision)\s*:?\s*(\d+)", line.strip(), re.IGNORECASE)
        if not rev_m:
            rev_m = re.match(r"^(\d+)\s+", line.strip())
        if rev_m:
            current_rev = int(rev_m.group(1))
            revisions.append({"revision": current_rev, "annotation": line.strip()})
        elif current_rev and revisions:
            revisions[-1]["annotation"] += " | " + line.strip()

    if not revisions:
        return f"Raw history (could not parse revisions):\n{history[:500]}"

    latest = max(r["revision"] for r in revisions)
    summary_lines = [f"Total revisions: {len(revisions)}, Latest: {latest}"]
    for r in revisions:
        marker = " ← CURRENT" if r["revision"] == latest else ""
        summary_lines.append(f"  Rev {r['revision']}: {r['annotation']}{marker}")

    if len(revisions) >= 2:
        prev = sorted([r["revision"] for r in revisions])[-2]
        summary_lines.append(f"\nSuggested rollback target: revision {prev} (most recent previous)")

    return "\n".join(summary_lines)


def _score_evidence_quality(evidence: Evidence) -> Tuple[int, List[str]]:
    """Score how complete the evidence is (0-100). If evidence is poor,
    GPT confidence should be capped — you can't be 95% sure with 30% evidence."""
    fields = [
        ("pod_status", 25, evidence.pod_status),
        ("deployment_describe", 20, evidence.deployment_describe),
        ("recent_events", 15, evidence.recent_events),
        ("rollout_history", 15, evidence.rollout_history),
        ("pod_logs", 10, evidence.pod_logs),
        ("resource_usage", 10, evidence.resource_usage),
        ("correlated_services", 5, evidence.correlated_services),
    ]
    score = 0
    missing = []
    for name, weight, value in fields:
        if value and value.strip() and value.strip() != "N/A":
            score += weight
        else:
            missing.append(name)
    return score, missing


def _detect_circular_remediation(service: str, namespace: str) -> Optional[str]:
    """Check outcome history for circular patterns (patch→rollback→patch→rollback)."""
    from backend.agent import outcome_tracker
    history = outcome_tracker.get_history(service, namespace)
    if len(history) < 3:
        return None

    recent = [h.action if hasattr(h, "action") else h.get("action", "") for h in history[-4:]]
    # Detect alternating patterns
    if len(recent) >= 4:
        if recent[-1] == recent[-3] and recent[-2] == recent[-4]:
            return (
                f"CIRCULAR REMEDIATION DETECTED: {recent[-4]}→{recent[-3]}→{recent[-2]}→{recent[-1]}. "
                f"Automated remediation is unlikely to resolve this — recommend human intervention."
            )
    # Detect same action repeated 3+ times
    if len(recent) >= 3 and len(set(recent[-3:])) == 1:
        return (
            f"REPEATED FAILURE: '{recent[-1]}' has been tried {len(recent)} times without success. "
            f"Automated remediation is unlikely to resolve this — recommend human intervention."
        )
    return None


def _validate_command(command: str, remediation_type: str, service: str,
                      namespace: str) -> Tuple[bool, str]:
    """Validate the GPT-generated kubectl command is syntactically safe.
    Returns (is_valid, error_message)."""
    if not command or remediation_type == "none":
        return True, ""

    # Must start with kubectl
    if not command.strip().startswith("kubectl "):
        return False, f"Command doesn't start with 'kubectl': {command[:50]}"

    # Must reference the correct namespace
    if f"-n {namespace}" not in command and f"--namespace={namespace}" not in command and f"--namespace {namespace}" not in command:
        return False, f"Command missing namespace '{namespace}'"

    # Block dangerous commands (use word boundaries to avoid false positives
    # e.g. "taint" must not match inside "dataintegrationservice")
    dangerous = ["delete namespace", "delete ns", "delete node", "cordon", "drain", "taint"]
    cmd_lower = command.lower()
    for d in dangerous:
        if re.search(r'\b' + re.escape(d) + r'\b', cmd_lower):
            return False, f"Dangerous operation blocked: '{d}'"

    # Type-specific checks
    if remediation_type == "rollback":
        if "rollout undo" not in command:
            return False, "Rollback command must use 'rollout undo'"
    elif remediation_type == "set_image":
        if "set image" not in command:
            return False, "set_image command must use 'set image'"
    elif remediation_type == "patch":
        if "set resources" not in command and "patch" not in command:
            return False, "Patch command must use 'set resources' or 'patch'"
        if "--containers" not in command and "set resources" in command:
            return False, "Patch with 'set resources' must include --containers"
    elif remediation_type == "restart":
        if "rollout restart" not in command:
            return False, "Restart command must use 'rollout restart'"
    elif remediation_type == "scale":
        if "scale" not in command or "--replicas" not in command:
            return False, "Scale command must use 'scale' with --replicas"

    return True, ""


# ── GPT Decision Engine prompt ───────────────────────────────────────────────

_DECISION_ENGINE_SYSTEM = """\
You are the Decision Engine for KIRA, an autonomous Kubernetes incident response system.
You receive analysis from specialist agents (SRE, App, Security, Cost) plus raw cluster evidence,
pre-computed health signals, and parsed rollout history.

Your job:
1. Check the PRE-ANALYSIS section first — use computed signals instead of re-parsing raw text
2. Synthesise all specialist findings — weight by confidence and evidence quality
3. Determine the DEFINITIVE root cause
4. Choose the optimal remediation action
5. Generate the EXACT kubectl command with real values from the evidence
6. Set confidence reflecting how certain you are, capped by evidence quality

You think like a senior SRE who has seen thousands of incidents. You don't guess — every claim
must be provable from the evidence provided. If the evidence is ambiguous, lower your confidence.

IMPORTANT: Respond with valid JSON only — no markdown, no extra text outside the JSON object."""

_DECISION_ENGINE_PROMPT = """\
INCIDENT: service '{service}' in namespace '{namespace}'
ALERT: {message}

=== PRE-ANALYSIS (computed from raw evidence — trust these signals) ===

--- HEALTH SUMMARY ---
{health_summary}

--- PARSED ROLLOUT HISTORY ---
{parsed_rollout}

--- EVIDENCE QUALITY ---
Score: {evidence_score}/100. {evidence_missing}
RULE: Your confidence MUST NOT exceed {confidence_cap} (= evidence_score + 10) because
incomplete evidence means you cannot be fully certain.

--- CIRCULAR REMEDIATION CHECK ---
{circular_check}

=== SPECIALIST AGENT OPINIONS ===
{agent_opinions}

=== RAW CLUSTER EVIDENCE ===

--- DEPLOYMENT/STATEFULSET DESCRIBE (current config) ---
{deployment_describe}

--- POD STATUS (live) ---
{pod_status}

--- RESOURCE USAGE (live metrics from kubectl top) ---
{resource_usage}

--- RECENT EVENTS (warnings from the cluster) ---
{recent_events}

--- POD LOGS (recent output) ---
{pod_logs}

--- ROLLOUT HISTORY (raw) ---
{rollout_history}

--- CORRELATED SERVICES ---
{correlated_services}

=== DEPLOYMENT INFO (structured) ===
{deployment_info_json}

=== OUTCOME HISTORY (past remediation attempts — do NOT repeat failed actions) ===
{outcome_history}

=== DECISION RULES ===

REMEDIATION TYPES (use the correct workload kind — deployment or statefulset):
  rollback  — "kubectl rollout undo <kind>/<name> -n <namespace> --to-revision=<N>{ctx}"
              When: CrashLoopBackOff, probe failures from recent config change, AND a known
              good previous revision exists in rollout history.
              CRITICAL: Always include --to-revision=<N>. Use the PARSED ROLLOUT HISTORY above
              to identify the last HEALTHY revision. If only 1 revision exists, rollback is NOT
              possible — choose restart or none instead.
              NOTE: Change-cause annotations may be identical across revisions while the actual
              container images differ between ReplicaSets. For ImagePullBackOff/ErrImagePull,
              ALWAYS try rollback to the previous revision — the old ReplicaSet likely has
              the correct image even if annotations look the same.
              WARNING: Do NOT rollback to the CURRENT revision — that changes nothing!
  set_image — "kubectl set image <kind>/<name> -n <namespace> <container>=<correct_image>{ctx}"
              When: ImagePullBackOff or ErrImagePull caused by a TYPO or wrong image name,
              AND you can determine the correct image name from the evidence (e.g. "ginx:1.27.0"
              should clearly be "nginx:1.27.0", or "myapp:v2.1" should be "myapp:v2.0.1").
              PREFER THIS over rollback when: the correct image is obvious from the typo,
              OR rollout history shows all revisions have the same bad image,
              OR there is only 1 revision (rollback impossible).
              Format: kubectl set image <kind>/<name> -n <namespace> <container>=<correct_image>{ctx}
  patch     — "kubectl set resources <kind>/<name> -n <namespace> --containers=<container> --limits=cpu=X,memory=Y --requests=cpu=X,memory=Y{ctx}"
              When: OOMKilled, resource limits too low vs actual usage
              RULES: Use comma-separated key=value, include --containers, use real values
              from evidence. Set request = ~50% of limit.
              NEVER patch to values that are already configured (check deployment info).
              For OOMKilled: new memory = max(actual_usage x 2, current_limit x 2), minimum 64Mi.
  restart   — "kubectl rollout restart <kind>/<name> -n <namespace>{ctx}"
              When: pod stuck/deadlocked but config is correct, pod in Terminating state
  scale     — "kubectl scale <kind>/<name> -n <namespace> --replicas=N{ctx}"
              When: not enough replicas, or need to scale down due to scheduling pressure
              WARNING: If HPA is active (check health summary), scaling will be overridden.
              In that case, prefer patching HPA minReplicas instead, or choose none.
  none      — No command needed
              When: deployment is healthy (ALL pods Running + Ready), or issue is outside
              cluster scope (node pressure, missing ConfigMap/Secret, PVC binding, DNS —
              these need human intervention)
              NEVER use 'none' when ANY pod is in a failing state (ImagePullBackOff,
              CrashLoopBackOff, ErrImagePull, Pending, ContainerCreating, etc.) —
              always attempt rollback or restart first.

CONFIDENCE GUIDELINES:
  95+  — Crystal clear: single obvious root cause, all agents agree, strong evidence, evidence_score ≥ 85
  85-94 — High: clear root cause, most agents agree, good evidence
  70-84 — Medium: likely root cause but some ambiguity, agents partially disagree
  50-69 — Low: uncertain, multiple possible causes, weak evidence — should require human approval
  <50   — Very low: guessing, contradictory evidence — definitely needs human review

EDGE CASE RULES:
  - HEALTHY DEPLOYMENT: If health summary shows all_running=true, zero_restarts=true,
    all_ready=true → remediation_type "none", confidence 95+. Do NOT invent problems.
  - INIT CONTAINER FAILURE: If init_container_failure=true, the INIT container is the
    problem, not the main container. Check events for the init container's error.
  - NODE PRESSURE: If node_pressure=true, check WHY scheduling is failing:
    • If the workload has MORE replicas than the cluster can schedule (desired >> ready)
      AND the running pods are healthy, the fix is to SCALE DOWN to a count that fits.
      Use remediation_type "scale" with --replicas=<ready_count> (the number already running).
    • If the replica count is reasonable but the node genuinely lacks capacity, set
      remediation_type "none" and explain that node-level intervention is needed.
  - CONFIG ERRORS: If config_error=true (missing ConfigMap/Secret/mount), pod-level
    remediation won't help. Set remediation_type "none" and cite the specific config error.
  - PVC ISSUES: If pvc_issues=true, pod can't mount storage. Set remediation_type "none"
    and explain the PVC/volume issue.
  - EVICTED PODS: If evicted_pods=true, check if the cause is node pressure or resource
    limits. Eviction from resource limits → patch; eviction from node pressure → none.
  - MULTI-CONTAINER PODS: If all_containers has multiple entries, identify WHICH container
    is failing from pod status/logs before choosing remediation. Use the correct --containers flag.
  - HPA ACTIVE: If hpa_active=true and you're considering "scale", warn that HPA will
    override manual scaling. Prefer "none" with a note about HPA configuration.
  - CIRCULAR REMEDIATION: If circular_check warns of repeated failures, set confidence
    below 50 and recommend human intervention.
  - STALE EVENTS: Events older than 10 minutes may be from a previous incident. Weight
    recent events (< 5 min) much higher than older ones.
  - ONLY 1 REVISION: If rollout history has only 1 revision, rollback is impossible —
    do NOT recommend rollback. Choose restart, patch, or none instead.

CRITICAL RULES:
  - NEVER hallucinate problems. Each claim must cite a specific line from the evidence.
  - If a previous remediation action FAILED (check outcome history), do NOT recommend the same action.
  - Your confidence MUST NOT exceed the confidence_cap from evidence quality section.
  - root_cause_points MUST be an array of plain strings, NOT objects.
  - STUCK ROLLOUT DETECTION: If ANY pod is Running but NOT fully Ready (e.g. "3/4 Ready"), this means
    a container is failing its readiness probe. This is NOT healthy — recommend rollback to restore the
    previous working ReplicaSet. Do NOT say "no action needed" when pods have partial readiness.

Respond ONLY with this JSON:
{{
  "root_cause": "<2 sentence summary of the primary root cause with specific values>",
  "root_cause_points": [
    "<evidence-backed finding 1 with specific values>",
    "<evidence-backed finding 2>",
    "<evidence-backed finding 3>"
  ],
  "contributing_factors": ["<secondary factor>"],
  "blast_radius": "<downstream impact or 'none'>",
  "affected_services": ["{service}"],
  "confidence": <integer 0-100>,
  "remediation_type": "<rollback|set_image|restart|scale|patch|none>",
  "remediation_reason": "<1 sentence: why this action fixes the root cause>",
  "remediation_command": "<exact kubectl command or empty string for none>"
}}"""


# ── JSON extraction (robust parser) ──────────────────────────────────────────

def _repair_truncated_json(text: str) -> str | None:
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


def _extract_json(text: str) -> dict:
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    if match:
        block = match.group()
        repaired = re.sub(
            r'"([^"\\]*(?:\\.[^"\\]*)*)"\s+([^,\]\}"]+?)\s*([,\]\}])',
            lambda m: f'"{m.group(1)} {m.group(2).strip()}"{m.group(3)}',
            block,
        )
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass

    if match:
        cleaned = re.sub(r'""', "'", match.group())
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

    if match:
        repaired = _repair_truncated_json(match.group())
        if repaired:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass

    raise ValueError(f"No valid JSON found in Decision Engine response: {text[:300]}")


# ── Evidence truncation helper ────────────────────────────────────────────────

def _safe(s: str | None, limit: int) -> str:
    text = (s or "N/A").replace('"', "'")
    if len(text) <= limit:
        return text
    error_keywords = ("error", "crashloop", "oomkill", "imagepull",
                      "pending", "backoff", "failed", "warning",
                      "createcontainer", "evicted", "0/")
    lines = text.splitlines()
    error_lines = [l for l in lines if any(k in l.lower() for k in error_keywords)]
    normal_lines = [l for l in lines if not any(k in l.lower() for k in error_keywords)]
    reordered = "\n".join(error_lines + normal_lines)
    return reordered[:limit]


# ── Main evaluate function ───────────────────────────────────────────────────

async def evaluate(
    evidence: Evidence,
    service: str,
    namespace: str,
    message: str,
    agent_opinions: List[AgentOpinion],
    queue: object,
) -> Tuple[Decision, RCAResult]:
    """Run the GPT Decision Engine with pre-analysis and post-validation.

    Pipeline:
      1. Pre-analysis: health summary, rollout parsing, evidence scoring,
         circular remediation check
      2. Fast-path: if clearly healthy, skip GPT entirely
      3. GPT call: with enriched prompt including pre-computed signals
      4. Post-validation: command syntax check, confidence capping
    """
    await queue.put(TimelineEvent(
        step="Decision Engine",
        status="running",
        detail="GPT Decision Engine analysing specialist opinions and evidence...",
    ))

    # ── Step 1: Pre-analysis ──────────────────────────────────────────────
    health = _compute_health_summary(evidence)
    parsed_rollout = _parse_rollout_history(evidence)
    evidence_score, evidence_missing = _score_evidence_quality(evidence)
    confidence_cap = min(100, evidence_score + 10)
    circular_warning = _detect_circular_remediation(service, namespace)

    logger.info(
        "Pre-analysis: health=%s, evidence_score=%d, confidence_cap=%d, circular=%s",
        {k: v for k, v in health.items() if v}, evidence_score, confidence_cap,
        circular_warning or "none",
    )

    # ── Step 2: Fast-path for clearly healthy deployments ─────────────────
    if (health["all_running"] and health["zero_restarts"] and health["all_ready"]
            and not health["crash_loop"] and not health["oom_killed"]
            and not health["image_pull_error"] and not health["pending_pods"]
            and not health.get("container_creating") and not health.get("sandbox_failure")
            and not health.get("warning_events")):
        logger.info("Fast-path: deployment is healthy — skipping GPT call")
        await queue.put(TimelineEvent(
            step="Decision Engine",
            status="success",
            detail=(
                "Fast-path: All pods Running, Ready, 0 restarts. "
                "No remediation needed — deployment is healthy."
            ),
        ))
        decision = Decision(
            remediation_type=RemediationType.NONE,
            confidence=97,
            reason="All pods are Running, Ready, with 0 restarts. Deployment is healthy.",
            pattern="gpt_engine_fast_path",
            command="",
            root_cause="No issue detected — deployment is healthy.",
        )
        rca_result = RCAResult(
            root_cause="No issue detected — deployment is healthy.",
            root_cause_points=["All pods Running and Ready", "Zero restarts", "Adequate resource limits"],
            contributing_factors=[],
            blast_radius="none",
            affected_services=[service],
            confidence=97,
            remediation_type=RemediationType.NONE,
            remediation_reason="Deployment is healthy — no action required.",
            remediation_command="",
        )
        return decision, rca_result

    # ── Step 3: Build prompt with enriched context ────────────────────────
    opinions_text = "\n\n".join(
        f"[{op.agent}] Confidence: {op.confidence}%\n"
        f"  Finding: {op.finding}\n"
        f"  Recommendation: {op.recommendation}\n"
        f"  Concerns: {', '.join(op.concerns) or 'none'}"
        for op in agent_opinions
    )

    from backend.agent import outcome_tracker
    history_text = outcome_tracker.format_history_for_llm(service, namespace)

    dep_info = _get_deployment_info(evidence)
    dep_info_json = json.dumps(dep_info, indent=2) if dep_info.get("deployment") else "N/A"

    ctx = _kube_context_flag()

    health_summary_text = json.dumps(
        {k: v for k, v in health.items() if v is not None and v is not False and v != 0},
        indent=2,
    )
    missing_text = f"Missing evidence: {', '.join(evidence_missing)}" if evidence_missing else "All evidence collected."
    circular_text = circular_warning or "No circular remediation pattern detected."

    prompt = _DECISION_ENGINE_PROMPT.format(
        service=service,
        namespace=namespace,
        message=message,
        health_summary=health_summary_text,
        parsed_rollout=parsed_rollout,
        evidence_score=evidence_score,
        evidence_missing=missing_text,
        confidence_cap=confidence_cap,
        circular_check=circular_text,
        agent_opinions=opinions_text,
        deployment_describe=_safe(evidence.deployment_describe, 800),
        pod_status=_safe(evidence.pod_status, 1500),
        pod_logs=_safe(evidence.pod_logs, 2000),
        resource_usage=_safe(evidence.resource_usage, 500),
        rollout_history=_safe(evidence.rollout_history, 800),
        recent_events=_safe(evidence.recent_events, 1000),
        correlated_services=_safe(evidence.correlated_services, 500),
        deployment_info_json=dep_info_json,
        outcome_history=history_text,
        ctx=ctx,
    )

    try:
        raw = await generate(prompt, system=_DECISION_ENGINE_SYSTEM)
        data = _extract_json(raw)

        # Normalize root_cause_points
        raw_points = data.get("root_cause_points", [])
        normalized_points = []
        for p in raw_points:
            if isinstance(p, str):
                normalized_points.append(p)
            elif isinstance(p, dict):
                val = (p.get("description") or p.get("finding")
                       or p.get("point") or next(iter(p.values()), ""))
                normalized_points.append(str(val))
            else:
                normalized_points.append(str(p))

        remediation_type = RemediationType(data.get("remediation_type", "none"))
        confidence = int(data.get("confidence", 50))
        root_cause = data.get("root_cause", "Root cause undetermined")
        reason = data.get("remediation_reason", "")
        command = data.get("remediation_command", "")

        # ── Step 4a: Cap confidence by evidence quality ───────────────────
        original_confidence = confidence
        if confidence > confidence_cap:
            logger.warning(
                "GPT confidence %d exceeds cap %d (evidence_score=%d) — capping",
                confidence, confidence_cap, evidence_score,
            )
            confidence = confidence_cap

        # If circular remediation detected, force low confidence
        if circular_warning and confidence > 45:
            logger.warning("Circular remediation detected — forcing confidence to 45")
            confidence = 45

        # ── Step 4b: Validate the generated command ───────────────────────
        cmd_valid, cmd_error = _validate_command(
            command, remediation_type.value, service, namespace
        )
        if not cmd_valid:
            logger.error("Command validation failed: %s — command: %s", cmd_error, command)
            await queue.put(TimelineEvent(
                step="Decision Engine",
                status="warning",
                detail=f"Command validation failed: {cmd_error}. Falling back to no action.",
            ))
            # Don't execute an invalid command — fall back to none with low confidence
            remediation_type = RemediationType.NONE
            confidence = min(confidence, 40)
            reason = f"Original command failed validation ({cmd_error}). Requires human review."
            command = ""

        decision = Decision(
            remediation_type=remediation_type,
            confidence=confidence,
            reason=reason,
            pattern="gpt_engine",
            command=command,
            root_cause=root_cause,
            root_cause_points=normalized_points,
            contributing_factors=data.get("contributing_factors", []),
            blast_radius=data.get("blast_radius", ""),
            affected_services=data.get("affected_services", [service]),
        )

        rca_result = RCAResult(
            root_cause=root_cause,
            root_cause_points=normalized_points,
            contributing_factors=data.get("contributing_factors", []),
            blast_radius=data.get("blast_radius"),
            affected_services=data.get("affected_services", [service]),
            confidence=confidence,
            remediation_type=remediation_type,
            remediation_reason=reason,
            remediation_command=command,
        )

        # Emit result with pre-analysis context
        cmd_display = command.replace("{}", namespace) if command else remediation_type.value
        cap_note = f" (capped from {original_confidence})" if original_confidence != confidence else ""
        await queue.put(TimelineEvent(
            step="Decision Engine",
            status="success",
            detail=(
                f"Verdict: {root_cause[:300]}  |  "
                f"Confidence: {confidence}%{cap_note}  |  "
                f"Evidence: {evidence_score}/100  |  "
                f"Action: {remediation_type.value}  |  "
                f"Command: {cmd_display}"
            ),
        ))

        logger.info(
            "GPT Decision Engine -> %s (confidence=%d%%, evidence=%d/100)",
            remediation_type.value, confidence, evidence_score,
        )

        return decision, rca_result

    except Exception as exc:
        logger.exception("GPT Decision Engine failed")
        await queue.put(TimelineEvent(
            step="Decision Engine",
            status="error",
            detail=f"Decision Engine failed: {exc}",
        ))
        raise
