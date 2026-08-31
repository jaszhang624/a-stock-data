"""Minimal Scheduler: orchestrate Coverage → Planner → Executor.

Pure orchestration component — coordinates the update pipeline without
executing data collection. Manually callable, deterministic, reuses
existing Planner and Executor with plan_materializations dedup.

Does NOT:
- modify Worker, SourceGovernor, adapters
- add cron, background threads, or daemon loops
- mutate state except through existing Executor
"""

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def run_update_cycle(
    store,
    engine,
    universe_path: str,
    reference_date: str | None = None,
    output_dir: str | None = None,
) -> dict:
    """Run a complete update cycle: Coverage → Planner → Executor.

    Args:
        store: DatasetStore instance with get_all_instrument_states().
        engine: JobEngine instance with create_job() and plan_materializations.
        universe_path: Path to instrument_universe_v2.json.
        reference_date: YYYY-MM-DD date for freshness assessment (default: today).
        output_dir: Directory for artifacts (coverage, plans). Defaults to engine.data_dir.

    Returns:
        Execution summary dict with coverage, plan, and materialization results.
    """
    from astock_api.run_lifecycle import (
        initialize_run_lifecycle, create_run, start_run, set_planned,
        set_executing, set_verifying, complete_run, fail_run, get_latest_run,
    )

    # Initialize lifecycle table and recover stale runs
    initialize_run_lifecycle(engine)

    if reference_date is None:
        reference_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if output_dir is None:
        output_dir = getattr(engine, "data_dir", "/tmp/scheduler-output")

    # Create run record
    run_id = create_run(engine, reference_date)

    try:
        # RUNNING — cycle started
        start_run(engine, run_id)

        # Step 1: Generate coverage snapshot
        from astock_api.coverage import generate_coverage_snapshot

        logger.info(f"Generating coverage snapshot for {reference_date}")
        coverage = generate_coverage_snapshot(
            store, universe_path, reference_date, output_dir=os.path.join(output_dir, "coverage")
        )

        # Step 2: Build update plan
        from astock_api.update_planner import FreshnessPolicy, UpdatePlanner

        policy = FreshnessPolicy(reference_date=reference_date)
        planner = UpdatePlanner(policy)

        logger.info("Creating update plan")
        plan = planner.create_plan(coverage)

        # PLANNED — plan generated (plan_hash will be set by executor)

        # Step 3: Materialize plan through Executor (with dedup)
        from astock_api.update_executor import materialize_plan, write_materialization_report

        logger.info("Materializing plan")
        report = materialize_plan(engine, plan.to_dict())

        # EXECUTING — jobs materialized
        created_jobs_count = len(report["created_jobs"])
        plan_hash = report["plan_hash"]
        set_executing(engine, run_id, jobs_created=created_jobs_count)

        # Step 4: Write artifacts
        report_path = write_materialization_report(report, output_dir=os.path.join(output_dir, "plans"))

        # Step 5: Write plan artifact
        plans_dir = os.path.join(output_dir, "plans")
        os.makedirs(plans_dir, exist_ok=True)
        plan_path = os.path.join(plans_dir, "latest_update_plan.json")
        with open(plan_path, "w") as f:
            f.write(plan.to_json())

        # Build execution summary
        plan_hash = report["plan_hash"]

        # Step 6: Verify run (VERIFYING → SUCCESS/FAILED)
        from astock_api.run_verifier import verify_run

        logger.info("Running verification gate")
        set_verifying(engine, run_id)
        verification = verify_run(engine, run_id, store=store, universe_path=universe_path)

        summary = {
            "run_id": run_id,
            "reference_date": reference_date,
            "coverage_summary": coverage.get("summary", {}),
            "plan_summary": plan.summary,
            "materialization": {
                "created_jobs": created_jobs_count,
                "skipped_actions": len(report.get("skipped_actions", [])),
                "duplicate_actions": len(report.get("duplicate_actions", [])),
                "already_materialized": report.get("already_materialized", False),
            },
            "plan_hash": plan_hash,
            "verification": verification.to_dict(),
            "artifacts": {
                "plan_path": plan_path,
                "report_path": report_path,
            },
        }

        if verification.status == "FAIL":
            error_msg = "; ".join(verification.errors)
            fail_run(engine, run_id, error_message=f"Verification failed: {error_msg}")
            logger.error(f"Update cycle verification failed: {error_msg}")
            raise RuntimeError(error_msg)

        # SUCCESS — cycle complete and verified
        quality_status = verification.checks.get("quality", {}).get("status") or "PASS"
        complete_run(engine, run_id, plan_hash=plan_hash, jobs_created=created_jobs_count, quality_status=quality_status)

        logger.info(f"Update cycle complete: {summary['materialization']['created_jobs']} jobs created, verification={verification.status}")
        return summary

    except Exception as e:
        # FAILED — do not hide exceptions
        fail_run(engine, run_id, error_message=str(e))
        logger.error(f"Update cycle failed: {e}")
        raise


def run_update_cycle_from_files(
    engine,
    universe_path: str = "/app/data/universe/instrument_universe_v2.json",
    reference_date: str | None = None,
) -> dict:
    """Run update cycle using default DatasetStore path.

    Args:
        engine: JobEngine instance.
        universe_path: Path to instrument_universe_v2.json.
        reference_date: YYYY-MM-DD date for freshness assessment.

    Returns:
        Execution summary dict.
    """
    from astock_api.dataset_store import DatasetStore

    store = DatasetStore("/app/data/astock_data.duckdb")
    return run_update_cycle(store, engine, universe_path, reference_date)
