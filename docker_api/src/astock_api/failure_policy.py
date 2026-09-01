"""Failure Classification and Recovery Policy.

Pure classification layer — does NOT modify Worker, JobEngine retry logic,
SourceGovernor, adapters, DatasetStore schema, scheduler lifecycle model,
planner logic, or executor logic.

Classifies job failures into categories and returns deterministic decisions:
- UPSTREAM_NO_DATA: severity=WARN, retryable=False, action=REPORT
- NETWORK_ERROR: severity=WARN, retryable=True, action=RETRY
- AUTH_ERROR: severity=ERROR, retryable=False, action=STOP
- IDENTITY_ERROR: severity=CRITICAL, retryable=False, action=STOP
- DATA_CORRUPTION: severity=CRITICAL, retryable=False, action=STOP
- SYSTEM_ERROR: severity=ERROR, retryable=False, action=INVESTIGATE
- UNKNOWN: severity=WARN, retryable=False, action=REPORT

Output: FailureDecision with category, severity, retryable, action.
"""

import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class FailureCategory(Enum):
    """Failure classification categories."""
    UPSTREAM_NO_DATA = "UPSTREAM_NO_DATA"
    NETWORK_ERROR = "NETWORK_ERROR"
    AUTH_ERROR = "AUTH_ERROR"
    IDENTITY_ERROR = "IDENTITY_ERROR"
    DATA_CORRUPTION = "DATA_CORRUPTION"
    SYSTEM_ERROR = "SYSTEM_ERROR"
    UNKNOWN = "UNKNOWN"


class Severity(Enum):
    """Failure severity levels."""
    WARN = "WARN"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class Action(Enum):
    """Recovery actions."""
    REPORT = "REPORT"       # Log and continue
    RETRY = "RETRY"         # Retry the job
    STOP = "STOP"           # Stop processing, requires human intervention
    INVESTIGATE = "INVESTIGATE"  # Flag for investigation


@dataclass(frozen=True)
class FailureDecision:
    """Deterministic decision for a failure category."""
    category: str
    severity: str
    retryable: bool
    action: str

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "severity": self.severity,
            "retryable": self.retryable,
            "action": self.action,
        }


# Deterministic policy table — no LLM, no heuristics.
POLICY_TABLE = {
    FailureCategory.UPSTREAM_NO_DATA: FailureDecision(
        category="UPSTREAM_NO_DATA",
        severity=Severity.WARN.value,
        retryable=False,
        action=Action.REPORT.value,
    ),
    FailureCategory.NETWORK_ERROR: FailureDecision(
        category="NETWORK_ERROR",
        severity=Severity.WARN.value,
        retryable=True,
        action=Action.RETRY.value,
    ),
    FailureCategory.AUTH_ERROR: FailureDecision(
        category="AUTH_ERROR",
        severity=Severity.ERROR.value,
        retryable=False,
        action=Action.STOP.value,
    ),
    FailureCategory.IDENTITY_ERROR: FailureDecision(
        category="IDENTITY_ERROR",
        severity=Severity.CRITICAL.value,
        retryable=False,
        action=Action.STOP.value,
    ),
    FailureCategory.DATA_CORRUPTION: FailureDecision(
        category="DATA_CORRUPTION",
        severity=Severity.CRITICAL.value,
        retryable=False,
        action=Action.STOP.value,
    ),
    FailureCategory.SYSTEM_ERROR: FailureDecision(
        category="SYSTEM_ERROR",
        severity=Severity.ERROR.value,
        retryable=False,
        action=Action.INVESTIGATE.value,
    ),
    FailureCategory.UNKNOWN: FailureDecision(
        category="UNKNOWN",
        severity=Severity.WARN.value,
        retryable=False,
        action=Action.REPORT.value,
    ),
}


# Keyword patterns for classification (order matters — first match wins).
CLASSIFICATION_PATTERNS = [
    # UPSTREAM_NO_DATA: source returned empty/no data
    (FailureCategory.UPSTREAM_NO_DATA, [
        "no data", "empty response", "not found", "symbol not found",
        "instrument not found", "no records", "data unavailable",
    ]),
    # NETWORK_ERROR: connectivity issues
    (FailureCategory.NETWORK_ERROR, [
        "timeout", "connection refused", "network error", "dns resolution",
        "socket error", "connection reset", "ssl certificate",
        "read timed out", "connect timeout", "max retries exceeded",
    ]),
    # AUTH_ERROR: authentication/authorization failures
    (FailureCategory.AUTH_ERROR, [
        "unauthorized", "forbidden", "401", "403", "token expired",
        "invalid token", "authentication failed", "access denied",
    ]),
    # IDENTITY_ERROR: canonical identity issues
    (FailureCategory.IDENTITY_ERROR, [
        "identity error", "canonical id mismatch", "ambiguous exchange",
        "invalid canonical", "exchange conflict", "identity collision",
    ]),
    # DATA_CORRUPTION: data integrity issues
    (FailureCategory.DATA_CORRUPTION, [
        "data corruption", "checksum mismatch", "invalid json",
        "parse error", "malformed response", "schema violation",
    ]),
]


def classify_failure(error_message: str | None) -> FailureCategory:
    """Classify a failure based on error message patterns.

    Args:
        error_message: The raw error message from the job.

    Returns:
        FailureCategory enum value.
    """
    if not error_message:
        return FailureCategory.UNKNOWN

    msg = error_message.lower()
    for category, patterns in CLASSIFICATION_PATTERNS:
        if any(pattern in msg for pattern in patterns):
            return category

    # Check for known HTTP status codes
    if "500" in msg or "internal server error" in msg:
        return FailureCategory.SYSTEM_ERROR

    if "502" in msg or "bad gateway" in msg:
        return FailureCategory.NETWORK_ERROR

    if "503" in msg or "service unavailable" in msg:
        return FailureCategory.NETWORK_ERROR

    if "504" in msg or "gateway timeout" in msg:
        return FailureCategory.NETWORK_ERROR

    return FailureCategory.UNKNOWN


def get_decision(category: FailureCategory) -> FailureDecision:
    """Get the deterministic decision for a failure category.

    Args:
        category: The classified FailureCategory.

    Returns:
        FailureDecision with severity, retryable flag, and action.
    """
    return POLICY_TABLE[category]


def classify_and_decide(error_message: str) -> FailureDecision:
    """Classify a failure and return its decision in one call.

    Args:
        error_message: The raw error message from the job.

    Returns:
        FailureDecision with category, severity, retryable flag, and action.
    """
    category = classify_failure(error_message)
    return get_decision(category)


def classify_failures_from_jobs(engine, job_ids: list[str] | None = None) -> list[FailureDecision]:
    """Classify all failed jobs and return decisions.

    Args:
        engine: JobEngine instance with _get_conn().
        job_ids: Optional explicit list of job IDs to scope the query to.
            When provided, only those jobs are examined. When None, all
            FAILED jobs are classified (global behavior).

    Returns:
        List of FailureDecision for each failed job.
    """
    conn = engine._get_conn()
    try:
        # Check if error_message column exists
        cur = conn.execute("PRAGMA table_info(jobs)")
        columns = [row[1] for row in cur.fetchall()]

        if "error_message" not in columns:
            # Fallback: use last_error column if available, else treat as SYSTEM_ERROR
            if "last_error" in columns:
                if job_ids:
                    placeholders = ",".join("?" * len(job_ids))
                    cur = conn.execute(
                        f"SELECT job_id, last_error FROM jobs WHERE status='FAILED' AND job_id IN ({placeholders})",
                        job_ids,
                    )
                else:
                    cur = conn.execute(
                        "SELECT job_id, last_error FROM jobs WHERE status='FAILED'"
                    )
                decisions = []
                for row in cur.fetchall():
                    job_id, error_msg = row
                    decision = classify_and_decide(error_msg or "")
                    decisions.append(decision)
                return decisions
            else:
                # No error column at all — treat all failed jobs as SYSTEM_ERROR
                if job_ids:
                    placeholders = ",".join("?" * len(job_ids))
                    cur = conn.execute(
                        f"SELECT COUNT(*) FROM jobs WHERE status='FAILED' AND job_id IN ({placeholders})",
                        job_ids,
                    )
                else:
                    cur = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='FAILED'")
                failed_count = cur.fetchone()[0] or 0
                return [get_decision(FailureCategory.SYSTEM_ERROR)] * failed_count

        if job_ids:
            placeholders = ",".join("?" * len(job_ids))
            cur = conn.execute(
                f"SELECT job_id, error_message FROM jobs WHERE status='FAILED' AND job_id IN ({placeholders})",
                job_ids,
            )
        else:
            cur = conn.execute(
                "SELECT job_id, error_message FROM jobs WHERE status='FAILED'"
            )
        decisions = []
        for row in cur.fetchall():
            job_id, error_msg = row
            decision = classify_and_decide(error_msg or "")
            decisions.append(decision)
        return decisions
    finally:
        conn.close()


def should_stop(decisions: list[FailureDecision]) -> bool:
    """Check if any decision requires stopping processing.

    Args:
        decisions: List of FailureDecision objects.

    Returns:
        True if any decision has action=STOP or severity=CRITICAL.
    """
    for d in decisions:
        if d.action == Action.STOP.value or d.severity == Severity.CRITICAL.value:
            return True
    return False


def should_retry(decisions: list[FailureDecision]) -> bool:
    """Check if any decision suggests retrying.

    Args:
        decisions: List of FailureDecision objects.

    Returns:
        True if any decision has retryable=True.
    """
    return any(d.retryable for d in decisions)


def summarize_failures(decisions: list[FailureDecision]) -> dict:
    """Summarize failure decisions by category.

    Args:
        decisions: List of FailureDecision objects.

    Returns:
        Dict with category counts and overall severity/action summary.
    """
    if not decisions:
        return {"total": 0, "categories": {}, "worst_severity": None, "actions": []}

    category_counts = {}
    worst_severity_order = {"WARN": 0, "ERROR": 1, "CRITICAL": 2}
    worst_severity = None
    actions = set()

    for d in decisions:
        cat = d.category
        category_counts[cat] = category_counts.get(cat, 0) + 1

        if worst_severity is None or worst_severity_order.get(d.severity, 0) > worst_severity_order.get(worst_severity, 0):
            worst_severity = d.severity

        actions.add(d.action)

    return {
        "total": len(decisions),
        "categories": category_counts,
        "worst_severity": worst_severity,
        "actions": sorted(actions),
    }
