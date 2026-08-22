"""Operation Report: unified daily operational snapshot.

Read-only aggregation layer — does NOT create new state, does NOT modify
existing modules. Aggregates truth from:

- Universe v2 artifact (instrument counts)
- Coverage snapshot (with_data, missing, current, stale)
- Freshness model (MISSING/CURRENT/STALE counts)
- Scheduler state (last run status, plan_hash, jobs_created)
- JobEngine database (job counts by status)

Output: data/reports/daily_operation_report.json
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def compute_universe_sha256(universe_path: str) -> str:
    """Compute SHA256 of the universe artifact file."""
    with open(universe_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def get_universe_summary(universe_path: str) -> dict:
    """Load universe and return aggregate counts by asset type.

    Args:
        universe_path: Path to instrument_universe_v2.json.

    Returns:
        Dict with total, equity, index counts.
    """
    from astock_api.coverage import load_universe

    instruments = load_universe(universe_path)
    equity_count = sum(1 for i in instruments if i.get("asset_type") == "EQUITY")
    index_count = sum(1 for i in instruments if i.get("asset_type") == "INDEX")

    return {
        "total": len(instruments),
        "equity": equity_count,
        "index": index_count,
    }


def get_coverage_summary(store, universe_path: str, reference_date: str, output_dir: str = "/app/data/coverage") -> dict:
    """Get coverage summary from existing coverage module.

    Args:
        store: DatasetStore instance.
        universe_path: Path to instrument_universe_v2.json.
        reference_date: YYYY-MM-DD date for freshness assessment.
        output_dir: Directory for coverage artifacts.

    Returns:
        Coverage summary dict (with_data, missing, current, stale).
    """
    from astock_api.coverage import generate_coverage_snapshot

    snapshot = generate_coverage_snapshot(
        store, universe_path, reference_date, output_dir=output_dir
    )
    return snapshot.get("summary", {})


def get_freshness_breakdown(store, universe_path: str, reference_date: str, output_dir: str = "/app/data/coverage") -> dict:
    """Get freshness state counts from coverage snapshot.

    Args:
        store: DatasetStore instance.
        universe_path: Path to instrument_universe_v2.json.
        reference_date: YYYY-MM-DD date for freshness assessment.
        output_dir: Directory for coverage artifacts.

    Returns:
        Dict with MISSING, CURRENT, STALE counts.
    """
    from astock_api.coverage import generate_coverage_snapshot

    snapshot = generate_coverage_snapshot(
        store, universe_path, reference_date, output_dir=output_dir
    )

    freshness_counts = {"MISSING": 0, "CURRENT": 0, "STALE": 0}
    for inst in snapshot.get("instruments", []):
        status = inst.get("freshness_status", "MISSING")
        if status in freshness_counts:
            freshness_counts[status] += 1

    return freshness_counts


def get_scheduler_summary(engine) -> dict:
    """Get latest scheduler run state.

    Args:
        engine: JobEngine instance.

    Returns:
        Dict with last_status, last_run_at, last_plan_hash, jobs_created.
    """
    from astock_api.scheduler_state import get_latest_run

    latest = get_latest_run(engine)
    if not latest:
        return {
            "last_status": None,
            "last_run_at": None,
            "last_plan_hash": None,
            "jobs_created": 0,
        }

    return {
        "last_status": latest["status"],
        "last_run_at": latest.get("started_at"),
        "last_plan_hash": latest.get("plan_hash"),
        "jobs_created": latest.get("created_jobs", 0),
    }


def get_job_counts(engine) -> dict:
    """Count jobs by status from JobEngine database.

    Args:
        engine: JobEngine instance.

    Returns:
        Dict with pending, running, done, failed counts.
    """
    conn = engine._get_conn()
    try:
        cur = conn.execute(
            "SELECT status, COUNT(*) FROM jobs GROUP BY status"
        )
        counts = {row[0]: row[1] for row in cur.fetchall()}
    finally:
        conn.close()

    return {
        "pending": counts.get("PENDING", 0),
        "running": counts.get("RUNNING", 0),
        "done": counts.get("DONE", 0),
        "failed": counts.get("FAILED", 0),
    }


def generate_daily_report(
    store,
    engine,
    universe_path: str,
    reference_date: str | None = None,
    output_dir: str | None = None,
) -> dict:
    """Generate the daily operation report.

    Aggregates data from existing modules without duplicating logic.

    Args:
        store: DatasetStore instance.
        engine: JobEngine instance.
        universe_path: Path to instrument_universe_v2.json.
        reference_date: YYYY-MM-DD date for freshness assessment (default: today).
        output_dir: Directory for artifacts. Defaults to engine.data_dir.

    Returns:
        Complete daily operation report dict.
    """
    if reference_date is None:
        reference_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if output_dir is None:
        output_dir = getattr(engine, "data_dir", "/tmp/report-output")

    universe_sha256 = compute_universe_sha256(universe_path)
    universe_summary = get_universe_summary(universe_path)
    coverage_summary = get_coverage_summary(store, universe_path, reference_date, output_dir=os.path.join(output_dir, "coverage"))
    freshness_breakdown = get_freshness_breakdown(store, universe_path, reference_date, output_dir=os.path.join(output_dir, "coverage"))
    scheduler_summary = get_scheduler_summary(engine)
    job_counts = get_job_counts(engine)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reference_date": reference_date,
        "universe_sha256": universe_sha256,
        "universe": universe_summary,
        "coverage": coverage_summary,
        "freshness": freshness_breakdown,
        "scheduler": scheduler_summary,
        "jobs": job_counts,
    }

    logger.info(f"Daily operation report generated for {reference_date}")
    return report


def write_daily_report(report: dict, output_dir: str = "/app/data/reports") -> str:
    """Write the daily operation report to disk.

    Args:
        report: The report dict from generate_daily_report().
        output_dir: Directory to write the artifact.

    Returns:
        Path to the written report file.
    """
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "daily_operation_report.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2)

    logger.info(f"Daily operation report written to {path}")
    return path


if __name__ == "__main__":
    import sys

    ref_date = sys.argv[1] if len(sys.argv) > 1 else None
    universe_path = sys.argv[2] if len(sys.argv) > 2 else "/app/data/universe/instrument_universe_v2.json"

    from astock_api.dataset_store import DatasetStore
    from astock_api.job_engine import JobEngine

    store = DatasetStore("/app/data/astock_data.duckdb")
    store.bootstrap()

    engine = JobEngine(db_path="/app/data/astock_jobs.db", data_dir="/app/data")
    engine.initialize()

    report = generate_daily_report(store, engine, universe_path, ref_date)
    path = write_daily_report(report)

    print(json.dumps(report, indent=2))
    print(f"\nReport written to: {path}")
