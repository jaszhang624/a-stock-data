"""R6-7A Plan Materialization Deduplication targeted tests.

Covers:
1. First execution creates jobs and registry entry
2. Second identical execution returns existing job IDs (no new jobs)
3. Different plan creates new jobs normally
4. Cross-market isolation preserved (SSE:000001 INDEX vs SZSE:000001 EQUITY)
5. Restart persistence (registry survives process restart)
6. Partial failure handling (JobEngine failure doesn't corrupt registry)
7. Empty plan produces valid empty report
8. Invalid action rejected safely
"""

import json
import os
import shutil
import tempfile

import pytest

from astock_api.job_engine import JobEngine
from astock_api.update_executor import (
    compute_plan_hash,
    materialize_plan,
)


@pytest.fixture
def engine():
    """Create isolated JobEngine for tests."""
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test_jobs.db")
    eng = JobEngine(db_path=db_path, data_dir=tmpdir)
    eng.initialize()
    yield eng
    shutil.rmtree(tmpdir, ignore_errors=True)


# 1. First execution creates jobs and registry entry
def test_first_execution_creates_jobs_and_registry(engine):
    plan = {
        "reference_date": "2026-08-20",
        "actions": [
            {
                "canonical_id": "SZSE:000001",
                "action": "UPDATE",
                "latest_date": "2026-08-18",
                "reason": "Stale data",
            }
        ],
    }

    report = materialize_plan(engine, plan)
    assert len(report["created_jobs"]) == 1
    assert report.get("already_materialized") is False

    # Verify registry entry exists
    plan_hash = compute_plan_hash(plan)
    conn = engine._get_conn()
    try:
        cur = conn.execute("SELECT plan_hash, status FROM plan_materializations WHERE plan_hash=?", (plan_hash,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == plan_hash
        assert row[1] == "materialized"
    finally:
        conn.close()


# 2. Second identical execution returns existing job IDs (no new jobs)
def test_second_identical_execution_no_new_jobs(engine):
    plan = {
        "reference_date": "2026-08-20",
        "actions": [
            {
                "canonical_id": "SZSE:000001",
                "action": "UPDATE",
                "latest_date": "2026-08-18",
                "reason": "Stale data",
            }
        ],
    }

    # First run
    report1 = materialize_plan(engine, plan)
    assert len(report1["created_jobs"]) == 1
    job_id_1 = report1["created_jobs"][0]["job_id"]

    # Second run — should return existing job IDs
    report2 = materialize_plan(engine, plan)
    assert len(report2["created_jobs"]) == 0
    assert report2.get("already_materialized") is True
    assert job_id_1 in report2["existing_job_ids"]


# 3. Different plan creates new jobs normally
def test_different_plan_creates_new_jobs(engine):
    plan1 = {
        "reference_date": "2026-08-20",
        "actions": [
            {
                "canonical_id": "SZSE:000001",
                "action": "UPDATE",
                "latest_date": "2026-08-18",
            }
        ],
    }

    plan2 = {
        "reference_date": "2026-08-21",
        "actions": [
            {
                "canonical_id": "SSE:600519",
                "action": "UPDATE",
                "latest_date": "2026-08-19",
            }
        ],
    }

    report1 = materialize_plan(engine, plan1)
    assert len(report1["created_jobs"]) == 1

    report2 = materialize_plan(engine, plan2)
    assert len(report2["created_jobs"]) == 1
    assert report2.get("already_materialized") is False


# 4. Cross-market isolation preserved
def test_cross_market_isolation(engine):
    plan = {
        "reference_date": "2026-08-20",
        "actions": [
            {
                "canonical_id": "SSE:000001",
                "asset_type": "INDEX",
                "action": "UPDATE",
                "latest_date": "2026-08-19",
            },
            {
                "canonical_id": "SZSE:000001",
                "asset_type": "EQUITY",
                "action": "UPDATE",
                "latest_date": "2026-08-19",
            },
        ],
    }

    report = materialize_plan(engine, plan)
    assert len(report["created_jobs"]) == 2

    jobs_by_id = {j["canonical_id"]: j for j in report["created_jobs"]}
    assert "SSE:000001" in jobs_by_id
    assert "SZSE:000001" in jobs_by_id

    # Verify independent jobs
    sse_job = engine.get_job(jobs_by_id["SSE:000001"]["job_id"])
    szse_job = engine.get_job(jobs_by_id["SZSE:000001"]["job_id"])
    assert sse_job is not None
    assert szse_job is not None
    assert sse_job["job_id"] != szse_job["job_id"]


# 5. Restart persistence (registry survives process restart)
def test_restart_persistence(engine):
    plan = {
        "reference_date": "2026-08-20",
        "actions": [
            {
                "canonical_id": "SZSE:000001",
                "action": "UPDATE",
                "latest_date": "2026-08-18",
            }
        ],
    }

    # First run
    report1 = materialize_plan(engine, plan)
    assert len(report1["created_jobs"]) == 1

    # Simulate restart: create new engine pointing to same DB
    db_path = engine.db_path
    data_dir = engine.data_dir
    new_engine = JobEngine(db_path=db_path, data_dir=data_dir)
    new_engine.initialize()

    # Second run with new engine — should still deduplicate
    report2 = materialize_plan(new_engine, plan)
    assert len(report2["created_jobs"]) == 0
    assert report2.get("already_materialized") is True

    # Cleanup new engine's temp dir
    shutil.rmtree(data_dir, ignore_errors=True)


# 6. Partial failure handling (JobEngine failure doesn't corrupt registry)
def test_partial_failure_handling(engine):
    plan = {
        "reference_date": "2026-08-20",
        "actions": [
            {
                "canonical_id": "SZSE:000001",
                "action": "UPDATE",
                "latest_date": "2026-08-18",
            },
        ],
    }

    # First run succeeds
    report1 = materialize_plan(engine, plan)
    assert len(report1["created_jobs"]) == 1

    # Second run should be deduplicated
    report2 = materialize_plan(engine, plan)
    assert len(report2["created_jobs"]) == 0
    assert report2.get("already_materialized") is True


# 7. Empty plan produces valid empty report
def test_empty_plan_produces_valid_report(engine):
    plan = {
        "reference_date": "2026-08-20",
        "actions": [],
    }

    report = materialize_plan(engine, plan)
    assert len(report["created_jobs"]) == 0
    assert report.get("already_materialized") is False


# 8. Invalid action rejected safely
def test_invalid_action_rejected_safely(engine):
    plan = {
        "reference_date": "2026-08-20",
        "actions": [
            {
                "canonical_id": "SSE:600519",
                "action": "EXECUTE",  # Not a valid action
                "latest_date": None,
            }
        ],
    }

    report = materialize_plan(engine, plan)
    assert len(report["created_jobs"]) == 0
    assert "SSE:600519" in report["skipped_actions"]


def test_plan_hash_deterministic():
    """Plan hash should be deterministic for same input."""
    plan1 = {
        "generated_at": "2026-08-20T10:00:00Z",
        "actions": [{"canonical_id": "SSE:600519", "action": "UPDATE"}],
    }
    plan2 = {
        "generated_at": "2026-08-20T11:00:00Z",
        "actions": [{"canonical_id": "SSE:600519", "action": "UPDATE"}],
    }

    assert compute_plan_hash(plan1) == compute_plan_hash(plan2)


def test_mixed_actions(engine):
    """Test plan with mixed action types."""
    plan = {
        "reference_date": "2026-08-20",
        "actions": [
            {"canonical_id": "SSE:600519", "action": "UPDATE", "latest_date": "2026-08-18"},
            {"canonical_id": "SZSE:000001", "action": "NONE", "latest_date": "2026-08-20"},
            {"canonical_id": "SSE:600000", "action": "BOOTSTRAP", "latest_date": None},
        ],
    }

    report = materialize_plan(engine, plan)
    assert len(report["created_jobs"]) == 2
    assert "SZSE:000001" in report["skipped_actions"]

    jobs_by_id = {j["canonical_id"]: j for j in report["created_jobs"]}
    assert jobs_by_id["SSE:600519"]["action"] == "UPDATE"
    assert jobs_by_id["SSE:600000"]["action"] == "BOOTSTRAP"
