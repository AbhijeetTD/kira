"""Outcome tracker — records what remediation was attempted and whether it
worked, providing a feedback loop for future decisions.

Stores a rolling history of (service, namespace, action, success) so the
decision engine and LLM can reference past attempts and avoid repeating
failed actions on the same workload.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

MAX_HISTORY = 100  # keep last N outcomes


@dataclass
class Outcome:
    service: str
    namespace: str
    action: str          # e.g. "rollback", "patch", "restart"
    pattern: str         # decision engine pattern or "llm_judge"
    success: bool
    confidence: int
    detail: str = ""     # brief explanation (root cause or error)
    timestamp: datetime = field(default_factory=datetime.utcnow)


# In-memory rolling history (sufficient for single-process demo)
_history: deque[Outcome] = deque(maxlen=MAX_HISTORY)


def record(service: str, namespace: str, action: str, pattern: str,
           success: bool, confidence: int, detail: str = "") -> None:
    """Record an outcome after validation completes."""
    outcome = Outcome(
        service=service, namespace=namespace, action=action,
        pattern=pattern, success=success, confidence=confidence,
        detail=detail,
    )
    _history.append(outcome)
    logger.info(
        "Outcome recorded: %s/%s action=%s pattern=%s success=%s confidence=%d",
        namespace, service, action, pattern, success, confidence,
    )


def get_history(service: str = "", namespace: str = "",
                limit: int = 10) -> list[Outcome]:
    """Return recent outcomes, optionally filtered by service/namespace."""
    results = []
    for o in reversed(_history):
        if service and o.service != service:
            continue
        if namespace and o.namespace != namespace:
            continue
        results.append(o)
        if len(results) >= limit:
            break
    return results


def last_failed_action(service: str, namespace: str) -> Optional[str]:
    """Return the last failed remediation action for this workload, if any.
    Used to avoid repeating the same failed action."""
    for o in reversed(_history):
        if o.service == service and o.namespace == namespace and not o.success:
            return o.action
    return None


def format_history_for_llm(service: str, namespace: str, limit: int = 5) -> str:
    """Format recent outcomes as text context for the LLM judge prompt."""
    history = get_history(service, namespace, limit)
    if not history:
        return "No previous remediation attempts for this workload."
    lines = []
    for o in history:
        status = "✓ succeeded" if o.success else "✗ failed"
        lines.append(
            f"  - {o.timestamp:%H:%M} {o.action} ({o.pattern}) → {status} "
            f"(confidence={o.confidence}%) {o.detail}"
        )
    return "Previous remediation attempts:\n" + "\n".join(lines)
