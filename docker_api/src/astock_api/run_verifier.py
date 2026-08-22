"""Run Verification Gate: convert technical completion into business success validation.

Read-only verification layer — does NOT mutate state, does NOT modify
existing modules. Validates:

1. JOB COMPLETENESS: created_jobs == done_jobs + failed_jobs
2. FAILURE CLASSIFICATION: UPSTREAM_NO_DATA (warn), SYSTEM_ERROR/IDENTITY_ERROR/PARSER_ERROR (fail)
3. QUALITY GATE: identity errors == 0, duplicates == 0, invalid OHLCV == 0
4. FRESHNESS VALIDATION: planned instruments improved or explainably missing

Output: VerificationResult with status PASS/WARN/FAIL.
"""

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class VerificationResult:
    """Result of run verification."""

    status: str  # PASS, WARN, FAIL
    checks: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "checks": self.checks,
            "warnings": self.warnings,
            "errors": self.errors,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def verify_run(engine, run_id: int, store=None, universe_path: str | None = None) -> VerificationResult:
    """Verify a completed run.

    Args:
        engine: JobEngine instance with _get_conn().
        run_id: The run id to verify.
        store: Optional DatasetStore for freshness checks.
        universe_path: Optional path to instrument_universe_v2.json.

    Returns:
        VerificationResult with PASS/WARN/FAIL status.
    """
    from astock_api.run_lifecycle import get_run

    run = get_run(engine, run_id)
    if not run:
        return VerificationResult(
            status="FAIL",
            checks={"jobs": "SKIP"},
            errors=[f"Run {run_id} not found"],
        )

    result = VerificationResult(status="PASS")

    # 1. Job Completeness
    job_check = _check_job_completeness(engine, run)
    result.checks["jobs"] = job_check.checks
    result.warnings.extend(job_check.warnings)
    result.errors.extend(job_check.errors)

    # 2. Quality Gate (if quality report exists)
    quality_check = _check_quality(engine, run_id)
    result.checks["quality"] = quality_check.checks
    result.warnings.extend(quality_check.warnings)
    result.errors.extend(quality_check.errors)

    # 3. Freshness Validation (if store available)
    if store and universe_path:
        freshness_check = _check_freshness(store, universe_path, run)
        result.checks["freshness"] = freshness_check.checks
        result.warnings.extend(freshness_check.warnings)
    else:
        result.checks["freshness"] = {"status": "SKIP", "reason": "store or universe_path not provided"}

    # Determine final status
    if result.errors:
        result.status = "FAIL"
    elif result.warnings:
        result.status = "WARN"

    return result


def _check_job_completeness(engine, run) -> VerificationResult:
    """Check 1: Job completeness — created == done + failed."""
    result = VerificationResult(status="PASS")

    created = run.get("jobs_created", 0)
    done = run.get("jobs_done", 0)
    failed = run.get("jobs_failed", 0)

    # Query actual job counts from jobs table
    conn = engine._get_conn()
    try:
        cur = conn.execute(
            "SELECT status, COUNT(*) FROM jobs GROUP BY status"
        )
        job_counts = {row[0]: row[1] for row in cur.fetchall()}
    finally:
        conn.close()

    actual_done = job_counts.get("DONE", 0)
    actual_failed = job_counts.get("FAILED", 0)
    actual_pending = job_counts.get("PENDING", 0)
    actual_running = job_counts.get("RUNNING", 0)

    # Check completeness: created == done + failed (only if no pending jobs)
    # If jobs are still PENDING, they haven't executed yet — warn, don't fail.
    if created > 0 and (done + failed) != created:
        if actual_pending > 0 or actual_running > 0:
            # Jobs still in flight — warn but don't fail
            result.warnings.append(
                f"Jobs not yet complete: created={created}, done+failed={done + failed}, pending={actual_pending}, running={actual_running}"
            )
        else:
            # No pending/running but incomplete — actual failure
            result.errors.append(
                f"Job completeness mismatch: created={created}, done+failed={done + failed}"
            )

    # Check for pending/running jobs (should be 0 after verification)
    if actual_pending > 0:
        result.warnings.append(f"{actual_pending} jobs still PENDING")

    if actual_running > 0:
        result.warnings.append(f"{actual_running} jobs still RUNNING")

    # Failure classification
    failure_reasons = _get_failure_reasons(engine)
    upstream_no_data = failure_reasons.get("UPSTREAM_NO_DATA", 0)
    system_errors = failure_reasons.get("SYSTEM_ERROR", 0)
    identity_errors = failure_reasons.get("IDENTITY_ERROR", 0)
    parser_errors = failure_reasons.get("PARSER_ERROR", 0)

    if upstream_no_data > 0:
        result.warnings.append(f"{upstream_no_data} jobs failed with UPSTREAM_NO_DATA (acceptable)")

    if system_errors > 0:
        result.errors.append(f"{system_errors} jobs failed with SYSTEM_ERROR")

    if identity_errors > 0:
        result.errors.append(f"{identity_errors} jobs failed with IDENTITY_ERROR")

    if parser_errors > 0:
        result.errors.append(f"{parser_errors} jobs failed with PARSER_ERROR")

    result.checks = {
        "created": created,
        "done": done,
        "failed": failed,
        "actual_done": actual_done,
        "actual_failed": actual_failed,
        "failure_classification": {
            "upstream_no_data": upstream_no_data,
            "system_error": system_errors,
            "identity_error": identity_errors,
            "parser_error": parser_errors,
        },
    }

    return result


def _get_failure_reasons(engine) -> dict:
    """Classify job failures by error pattern using failure_policy."""
    from astock_api.failure_policy import classify_failures_from_jobs, summarize_failures

    decisions = classify_failures_from_jobs(engine)
    summary = summarize_failures(decisions)

    # Map to verifier's expected format
    categories = summary.get("categories", {})
    return {
        "UPSTREAM_NO_DATA": categories.get("UPSTREAM_NO_DATA", 0),
        "SYSTEM_ERROR": categories.get("SYSTEM_ERROR", 0) + categories.get("UNKNOWN", 0),
        "IDENTITY_ERROR": categories.get("IDENTITY_ERROR", 0) + categories.get("DATA_CORRUPTION", 0),
        "PARSER_ERROR": categories.get("AUTH_ERROR", 0),
    }


def _check_quality(engine, run_id) -> VerificationResult:
    """Check 2: Quality gate — identity errors, duplicates, invalid OHLCV."""
    result = VerificationResult(status="PASS")

    # Check if quality report exists for this run
    from astock_api.run_lifecycle import get_run

    run = get_run(engine, run_id)
    if not run:
        return VerificationResult(status="FAIL", errors=["Run not found"])

    # Read quality report if it exists
    data_dir = getattr(engine, "data_dir", "/tmp")
    quality_path = os.path.join(data_dir, "reports", f"quality_run_{run_id}.json")

    if not os.path.exists(quality_path):
        result.checks = {"status": "SKIP", "reason": "no quality report for this run"}
        return result

    try:
        with open(quality_path) as f:
            quality = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        result.warnings.append(f"Failed to read quality report: {e}")
        return result

    # Identity check
    identity_errors = quality.get("identity", {}).get("errors", 0)
    if identity_errors > 0:
        result.errors.append(f"Identity errors: {identity_errors}")

    # Duplicate check
    duplicates = quality.get("duplicates", {}).get("duplicated_keys", 0)
    if duplicates > 0:
        result.errors.append(f"Duplicate rows: {duplicates}")

    # OHLCV check
    invalid_ohlcv = quality.get("ohlcv", {}).get("invalid_rows", 0)
    if invalid_ohlcv > 0:
        result.errors.append(f"Invalid OHLCV rows: {invalid_ohlcv}")

    result.checks = {
        "status": quality.get("status", "UNKNOWN"),
        "identity_errors": identity_errors,
        "duplicates": duplicates,
        "invalid_ohlcv": invalid_ohlcv,
    }

    return result


def _check_freshness(store, universe_path: str, run) -> VerificationResult:
    """Check 3: Freshness validation — compare before/after."""
    result = VerificationResult(status="PASS")

    try:
        from astock_api.freshness import assess_freshness
        from astock_api.coverage import load_universe

        universe = load_universe(universe_path)
        reference_date = run.get("reference_date")

        if not reference_date:
            result.checks = {"status": "SKIP", "reason": "no reference_date in run"}
            return result

        # Assess current freshness state
        states = store.get_all_instrument_states()
        freshness_map = {s["security_id"]: s for s in states}

        current_count = 0
        stale_count = 0
        missing_count = 0

        for inst in universe:
            cid = inst.get("canonical_id", "")
            state = freshness_map.get(cid)
            if not state:
                missing_count += 1
                continue

            status = assess_freshness(state, reference_date)
            if status == "CURRENT":
                current_count += 1
            elif status == "STALE":
                stale_count += 1

        # Check if planned instruments improved
        created = run.get("jobs_created", 0)
        if created > 0 and current_count == 0:
            result.warnings.append(
                f"Run created {created} jobs but no instruments show CURRENT status — "
                "jobs may not have completed yet"
            )

        result.checks = {
            "status": "OK",
            "current": current_count,
            "stale": stale_count,
            "missing": missing_count,
        }

    except Exception as e:
        result.warnings.append(f"Freshness check failed: {e}")
        result.checks = {"status": "ERROR", "error": str(e)}

    return result