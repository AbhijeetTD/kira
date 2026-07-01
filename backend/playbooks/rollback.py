"""Playbook: roll back a workload (Deployment or StatefulSet) to its previous revision.

Smart rollback — scans revision history to find the last revision whose
container image differs from the current broken one, then rolls back to
THAT specific revision rather than blindly going one step back.  This
prevents the case where recent patch/scale operations created new revisions
that all share the same broken image.

For StatefulSets, the rollback also deletes crashing pods after reverting the
spec so they are recreated with the corrected template (StatefulSet controller
with OrderedReady policy can deadlock otherwise).
"""
from __future__ import annotations

import logging
import re
import subprocess
import time

logger = logging.getLogger(__name__)


def _kubectl_base() -> list[str]:
    from backend.config import settings  # noqa: PLC0415
    cmd = ["kubectl"]
    if settings.kube_context:
        cmd += ["--context", settings.kube_context]
    return cmd


def _resolve_kind(name: str, namespace: str) -> str:
    """Detect whether the workload is a Deployment or StatefulSet."""
    from backend.integrations.k8s_client import resolve_workload_kind  # noqa: PLC0415
    return resolve_workload_kind(name, namespace)


def _current_images(deployment: str, namespace: str) -> set[str]:
    """Return the set of container images currently in the workload spec."""
    kind = _resolve_kind(deployment, namespace)
    result = subprocess.run(
        _kubectl_base() + [
            "get", f"{kind}/{deployment}", "-n", namespace,
            "-o", "jsonpath={.spec.template.spec.containers[*].image}",
        ],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        return set()
    return set(result.stdout.strip().split())


def _revision_images(deployment: str, namespace: str, revision: int) -> set[str]:
    """Return container images for a specific rollout revision."""
    kind = _resolve_kind(deployment, namespace)
    result = subprocess.run(
        _kubectl_base() + [
            "rollout", "history", f"{kind}/{deployment}",
            "-n", namespace, f"--revision={revision}",
        ],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        return set()
    # Extract image lines from the describe-style output
    images: set[str] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("Image:"):
            images.add(line.split(":", 1)[1].strip())
    return images


def _max_revision(deployment: str, namespace: str) -> int:
    """Return the highest revision number in the rollout history."""
    kind = _resolve_kind(deployment, namespace)
    result = subprocess.run(
        _kubectl_base() + [
            "rollout", "history", f"{kind}/{deployment}", "-n", namespace,
        ],
        capture_output=True, text=True, timeout=10,
    )
    revisions = [
        int(m.group(1))
        for line in result.stdout.splitlines()
        if (m := re.match(r"^\s*(\d+)\s+", line))
    ]
    return max(revisions) if revisions else 0


def _find_good_revision(deployment: str, namespace: str) -> int | None:
    """Scan history from newest to oldest to find the last revision whose
    images differ from the current (broken) ones.  Returns that revision
    number, or None if no different revision is found."""
    broken_images = _current_images(deployment, namespace)
    if not broken_images:
        return None

    max_rev = _max_revision(deployment, namespace)
    if max_rev <= 1:
        return None

    # Check from max_rev-1 down to 1
    for rev in range(max_rev - 1, 0, -1):
        rev_images = _revision_images(deployment, namespace, rev)
        if rev_images and rev_images != broken_images:
            logger.info(
                "Found good revision %d for %s/%s: images=%s (broken=%s)",
                rev, namespace, deployment, rev_images, broken_images,
            )
            return rev

    return None  # all revisions share the same image — simple undo is the best we can do


def _cleanup_crashing_pods(deployment: str, namespace: str, kind: str) -> str:
    """For StatefulSets: delete pods still running the old (broken) template
    so the controller recreates them with the reverted spec.  StatefulSet
    controllers with OrderedReady policy can deadlock if a crashing pod
    blocks the rolling update."""
    if kind != "statefulset":
        return ""

    # Wait a moment for the spec to propagate
    time.sleep(3)

    # Find pods that are not ready / crashing
    result = subprocess.run(
        _kubectl_base() + [
            "get", "pods", "-n", namespace,
            "-l", f"app={deployment}",
            "-o", "jsonpath={range .items[?(@.status.containerStatuses[0].ready==false)]}{.metadata.name}{\"\\n\"}{end}",
        ],
        capture_output=True, text=True, timeout=10,
    )
    bad_pods = [p.strip() for p in result.stdout.strip().splitlines() if p.strip()]

    if not bad_pods:
        return ""

    deleted = []
    for pod in bad_pods:
        del_result = subprocess.run(
            _kubectl_base() + ["delete", "pod", pod, "-n", namespace, "--grace-period=0", "--force"],
            capture_output=True, text=True, timeout=15,
        )
        if del_result.returncode == 0:
            deleted.append(pod)
            logger.info("Force-deleted crashing pod %s for StatefulSet rollback", pod)

    return f"  (deleted {len(deleted)} crashing pod(s): {', '.join(deleted)})" if deleted else ""


def execute(deployment: str, namespace: str, pattern: str = "") -> tuple[bool, str]:
    """Execute a rollback. Works for both Deployments and StatefulSets.

    *pattern* (optional) is the decision-engine pattern that triggered this
    rollback.  For ``image_pull_error`` we do the smart revision scan
    (find a revision with a different image).  For everything else we
    simply undo one step — this avoids accidentally rolling back to an
    OLD bad revision that happens to have a different image.
    """
    kind = _resolve_kind(deployment, namespace)
    good_rev = None
    if pattern == "image_pull_error":
        good_rev = _find_good_revision(deployment, namespace)

    if good_rev:
        cmd = _kubectl_base() + [
            "rollout", "undo", f"{kind}/{deployment}",
            "-n", namespace, f"--to-revision={good_rev}",
        ]
        logger.info("Smart rollback to revision %d: %s", good_rev, " ".join(cmd))
    else:
        # Simple undo one step — safest for probe/crashloop/config issues
        cmd = _kubectl_base() + [
            "rollout", "undo", f"{kind}/{deployment}", "-n", namespace,
        ]
        logger.info("Simple rollback (undo one step): %s", " ".join(cmd))

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    success = result.returncode == 0
    output = result.stdout.strip() if success else result.stderr.strip()

    if success and good_rev:
        output = f"{output}  (rolled back to revision {good_rev} with known-good image)"

    # StatefulSet: force-delete crashing pods so they get recreated with the reverted spec
    if success:
        cleanup_msg = _cleanup_crashing_pods(deployment, namespace, kind)
        if cleanup_msg:
            output += cleanup_msg

    return success, output or "No output."


def command_preview(deployment: str, namespace: str, pattern: str = "") -> str:
    kind = _resolve_kind(deployment, namespace)
    if pattern == "image_pull_error":
        good_rev = _find_good_revision(deployment, namespace)
        if good_rev:
            return f"kubectl rollout undo {kind}/{deployment} -n {namespace} --to-revision={good_rev}"
    return f"kubectl rollout undo {kind}/{deployment} -n {namespace}"

