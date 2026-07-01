"""LLM-driven cluster health monitor.

Instead of hardcoding what "wrong" looks like, this monitor collects raw
cluster state (pods, deployments, nodes, events, resource usage) and asks
the LLM to identify any issues it sees — covering every edge case the LLM
knows about, not just the ones we thought to enumerate.

Flow every INTERVAL seconds:
  1. Collect raw kubectl/SDK output across the cluster
  2. Send to LLM: "here is the cluster state — what needs attention?"
  3. Parse the JSON array returned: [{workload, namespace, message}]
  4. Fire a KIRA incident for each new issue found
  5. Clear alerts for workloads no longer flagged as unhealthy
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from typing import Awaitable, Callable

from backend.config import settings

logger = logging.getLogger(__name__)

# System namespaces excluded from raw data collection
_SKIP_NS = {"kube-system", "kube-public", "kube-node-lease"}

# Already-alerted (namespace, workload) pairs — cleared when the LLM no longer
# reports that workload as unhealthy.
_alerted: set[tuple[str, str]] = set()

# ── System prompt for the LLM ───────────────────────────────────────────────────

_SYSTEM = """\
You are an expert Kubernetes SRE and cluster reliability engineer.
Your job is to analyse raw cluster state data and identify workloads that
have REAL, PERSISTENT problems requiring human or automated intervention.

STRICT RULES — follow all of them:
1. NEVER report individual pod names. Always report the owning Deployment,
   StatefulSet, DaemonSet, or Job name. Strip hash suffixes from pod names
   to get the controller: e.g. "cart-web-7fb57894dc-8llwq" → "cart-web".
2. Report ONE entry per workload, even if many pods of that workload are
   affected. Consolidate all pod-level evidence into a single message.
3. SKIP namespaces: kube-system, kube-public, kube-node-lease.
   Only include node-level issues (e.g. NotReady, DiskPressure) with
   namespace "cluster" and workload "node/<node-name>".
4. IGNORE transient / self-healing conditions: a single probe failure that
   is not repeated, brief ContainerCreating, or pods that also show Running
   status in the same data set.
5. ONLY flag issues that are clearly persistent and need action:
   CrashLoopBackOff, OOMKilled, ImagePullBackOff, repeated probe failures
   across multiple pods, unavailable replicas, node pressure, job failures.
   ALSO flag STALLED ROLLOUTS: if a deployment shows fewer ready replicas
   than desired (e.g. READY 1/3) and pods are Running but not Ready, the
   rolling update is stuck — this IS a persistent problem that needs action.
6. Do NOT create incidents for healthy workloads or system infrastructure
   that is functioning normally.

Respond ONLY with a valid JSON array (no markdown, no explanation).
Each element must have exactly these keys:
  "workload"  — controller name (Deployment/StatefulSet/DaemonSet/Job/node)
  "namespace" — kubernetes namespace
  "message"   — concise description of the persistent problem and its
                 likely root cause (2-3 sentences, include affected pod count)

Return [] if nothing is persistently broken.
""".strip()


# ── Workload name normaliser ─────────────────────────────────────────────────

_POD_HASH_RE = re.compile(r"-[a-z0-9]{7,10}-[a-z0-9]{4,6}$")
_RS_HASH_RE  = re.compile(r"-[a-z0-9]{7,10}$")


def _normalize_workload(name: str) -> str:
    """Strip pod/replicaset hash suffixes so individual pod names become
    their parent deployment name.
    e.g. cart-web-7fb57894dc-8llwq  → cart-web
         cart-web-7fb57894dc        → cart-web
    """
    name = _POD_HASH_RE.sub("", name)  # strip pod suffix first
    name = _RS_HASH_RE.sub("", name)   # then replicaset suffix if still present
    return name


# ── Raw cluster state collectors ──────────────────────────────────────────────

def _kubectl(*args: str) -> str:
    """Run a kubectl command and return stdout as text (empty string on error)."""
    cmd = ["kubectl"]
    if settings.kube_context:
        cmd += ["--context", settings.kube_context]
    cmd += list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _collect_raw_state() -> str:
    """Gather a concise, text-based snapshot of the cluster for the LLM to read."""
    namespaces_cfg = settings.memory_monitor_namespaces.strip()
    target_ns = [n.strip() for n in namespaces_cfg.split(",") if n.strip()]
    ns_flags = []
    for ns in target_ns:
        ns_flags += ["-n", ns]
    all_ns_flag = ["--all-namespaces"] if not target_ns else []

    sections: list[str] = []

    # Pods — all non-system namespaces
    pods = _kubectl("get", "pods", *(all_ns_flag or ns_flags),
                    "--field-selector=metadata.namespace!=kube-system,"
                    "metadata.namespace!=kube-public,"
                    "metadata.namespace!=kube-node-lease",
                    "-o", "wide") if not target_ns else \
           _kubectl("get", "pods", *ns_flags, "-o", "wide")
    if pods:
        sections.append("## Pod Status\n" + pods)

    # Deployments
    deps = (_kubectl("get", "deployments", *all_ns_flag, "-o", "wide") if not target_ns
            else _kubectl("get", "deployments", *ns_flags, "-o", "wide"))
    if deps:
        sections.append("## Deployments\n" + deps)
        # Highlight stalled rollouts — where READY < DESIRED or UP-TO-DATE < DESIRED
        # This helps the LLM detect stuck rolling updates (e.g. bad probe port)
        stalled_lines = []
        for line in deps.splitlines()[1:]:  # skip header
            cols = line.split()
            if len(cols) >= 5:
                ready_col = cols[1] if "/" in cols[1] else ""
                if ready_col:
                    parts = ready_col.split("/")
                    if len(parts) == 2:
                        try:
                            ready, desired = int(parts[0]), int(parts[1])
                            name = cols[0]
                            if ready < desired:
                                stalled_lines.append(
                                    f"  ⚠ {name}: only {ready}/{desired} replicas ready — "
                                    f"rolling update may be stalled")
                        except ValueError:
                            pass
        if stalled_lines:
            sections.append("## Stalled Rollouts Detected\n" + "\n".join(stalled_lines))

    # StatefulSets
    sts = (_kubectl("get", "statefulsets", *all_ns_flag, "-o", "wide") if not target_ns
           else _kubectl("get", "statefulsets", *ns_flags, "-o", "wide"))
    if sts:
        sections.append("## StatefulSets\n" + sts)

    # Recent warning events (last 30, non-system)
    events_raw = (_kubectl("get", "events", *all_ns_flag,
                            "--field-selector=type=Warning",
                            "--sort-by=.lastTimestamp") if not target_ns
                  else _kubectl("get", "events", *ns_flags,
                                "--field-selector=type=Warning",
                                "--sort-by=.lastTimestamp"))
    if events_raw:
        # Keep last 30 lines to avoid overflowing the context window
        event_lines = events_raw.splitlines()
        sections.append("## Recent Warning Events (last 30)\n" +
                        "\n".join(event_lines[-30:]))

    # Nodes
    nodes = _kubectl("get", "nodes", "-o", "wide")
    if nodes:
        sections.append("## Nodes\n" + nodes)

    # Resource usage (best-effort — metrics-server may be absent)
    top_nodes = _kubectl("top", "nodes", "--no-headers")
    if top_nodes:
        sections.append("## Node Resource Usage\n" + top_nodes)

    top_pods = (_kubectl("top", "pods", *all_ns_flag, "--no-headers") if not target_ns
                else _kubectl("top", "pods", *ns_flags, "--no-headers"))
    if top_pods:
        sections.append("## Pod Resource Usage\n" + top_pods)

    if not sections:
        return "(no cluster data available)"

    return "\n\n".join(sections)


# ── LLM analysis ───────────────────────────────────────────────────────────

def _extract_json(text: str) -> list[dict]:
    """Pull a JSON array out of the LLM's response however it is formatted."""
    text = text.strip()
    # Strategy 1: entire response is valid JSON
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass
    # Strategy 2: find the first [...] block
    m = re.search(r"\[.*?\]", text, re.DOTALL)
    if m:
        try:
            result = json.loads(m.group(0))
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass
    # Strategy 3: LLM said nothing or garbled response — treat as no issues
    logger.debug("Cluster monitor: LLM response not parseable as JSON array: %s", text[:200])
    return []


async def _ask_llm(raw_state: str) -> list[dict]:
    """Ask the LLM to identify unhealthy workloads and return structured issues."""
    from backend.integrations.openai_client import generate  # noqa: PLC0415
    prompt = (
        "Analyse the Kubernetes cluster state below and return a JSON array of "
        "any unhealthy workloads as described in your instructions.\n\n"
        f"{raw_state}"
    )
    try:
        response = await generate(prompt, system=_SYSTEM)
        return _extract_json(response)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cluster monitor: LLM call failed: %s", exc)
        return []


# ── Recovery detection ────────────────────────────────────────────────────────

async def _resolve_recovered(current_issues: list[dict]) -> None:
    """Clear _alerted entries for workloads the LLM no longer reports as unhealthy."""
    flagged_now = {
        (item.get("namespace", ""), _normalize_workload(item.get("workload", "")))
        for item in current_issues
        if item.get("namespace", "") not in ("kube-system", "kube-public", "kube-node-lease")
    }
    recovered = _alerted - flagged_now
    for key in recovered:
        _alerted.discard(key)
        logger.info("Cluster monitor: %s/%s recovered — alert cleared", key[0], key[1])


# ── Core loop ─────────────────────────────────────────────────────────────────

async def _check_once(
    create_incident: Callable[[str, str, str], Awaitable[None]],
) -> None:
    loop = asyncio.get_event_loop()

    # Collect raw state in a thread (subprocess calls)
    raw_state = await loop.run_in_executor(None, _collect_raw_state)
    logger.info("Cluster monitor: raw state sections: %s",
                [l for l in raw_state.splitlines() if l.startswith("##")])

    # Ask LLM to analyse it
    issues = await _ask_llm(raw_state)
    logger.info("Cluster monitor: LLM returned %d issue(s): %s",
                len(issues), json.dumps(issues)[:500])

    # Clear alerts for workloads that are healthy again
    await _resolve_recovered(issues)

    # Deduplicate and normalise — one incident per (namespace, deployment)
    seen: dict[tuple[str, str], str] = {}
    for item in issues:
        raw_workload = item.get("workload", "").strip()
        namespace    = item.get("namespace", "").strip()
        message      = item.get("message", "").strip()

        if not raw_workload or not namespace or not message:
            continue

        # Skip system namespaces that slipped through the prompt filter
        if namespace in ("kube-system", "kube-public", "kube-node-lease"):
            continue

        workload = _normalize_workload(raw_workload)
        key = (namespace, workload)

        # Merge messages for the same workload into the first one
        if key not in seen:
            seen[key] = message
        else:
            # Append extra detail from duplicate entries (different pod evidence)
            seen[key] = seen[key] + " | " + message

    for key, message in seen.items():
        namespace, workload = key
        if key in _alerted:
            continue

        logger.warning("Cluster monitor [LLM]: %s/%s — %s", namespace, workload, message[:140])
        _alerted.add(key)
        await create_incident(workload, namespace, message)


async def crash_monitor_loop(
    create_incident: Callable[[str, str, str], Awaitable[None]],
) -> None:
    """Long-running background task. Call once at app startup."""
    interval = settings.memory_monitor_interval_s
    logger.info("Cluster health monitor (LLM-driven) started — interval=%ds", interval)

    # Ensure kubeconfig is patched for Docker (127.0.0.1 → host.docker.internal)
    # so kubectl subprocess calls reach the host cluster.
    from backend.integrations.k8s_client import _load_kube_config  # noqa: PLC0415
    _load_kube_config()

    await asyncio.sleep(25)  # stagger after memory monitor

    while True:
        await _check_once(create_incident)
        await asyncio.sleep(interval)

