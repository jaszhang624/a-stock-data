"""Regression tests for run-scoped job verification (fix/run-status-job-scoping).

Covers:
1. Historical failed job + current run with zero jobs → NOT failed
2. Previous cycle's failed jobs + second identical plan dedups to zero → NOT failed
3. Current run owns a failed job → correctly reports failure
4. Unrelated job created while run is active → does NOT contaminate
5. Existing successful-run behavior remains correct
"""

import json
import os
import pytest


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """Create isolated JobEngine for tests."""
    from astock_api.job_engine import JobEngine

    db_path = str(tmp_path / "jobs.db")
    data_dir = str(tmp_path / "data")
    monkeypatch.setattr(
        JobEngine, "__init__",
        lambda self: setattr(self, "db_path", db_path)
        or setattr(self, "data_dir", data_dir),
    )
    engine = JobEngine()
    engine.initialize()
    return engine


def _make_update_params(code, exchange="SSE", asset_type="EQUITY"):
    return {
        "instruments": [{"code": code, "exchange": exchange, "asset_type": asset_type}],
        "count": 10,
    }


def _create_and_fail_job(engine, code="600519"):
    """Create a job and mark it FAILED with a SYSTEM_ERROR."""
    engine.create_job("market_bars_update", _make_update_params(code))
    conn = engine._get_conn()
    try:
        conn.execute(
            "UPDATE jobs SET status='FAILED', last_error='all sources unavailable for test' "
            "WHERE status='PENDING'"
        )
        conn.commit()
    finally:
        conn.close()


def _create_and_succeed_job(engine, code="600519"):
    """Create a job and mark it DONE."""
    engine.create_job("market_bars_update", _make_update_params(code))
    conn = engine._get_conn()
    try:
        conn.execute(
            "UPDATE jobs SET status='DONE', completed_chunks=1, failed_chunks=0 "
            "WHERE status='PENDING'"
        )
        conn.commit()
    finally:
        conn.close()


def _get_job_ids(engine, limit=None):
    """Get job IDs from the engine."""
    conn = engine._get_conn()
    try:
        if limit:
            cur = conn.execute("SELECT job_id FROM jobs ORDER BY created_at LIMIT ?", (limit,))
        else:
            cur = conn.execute("SELECT job_id FROM jobs ORDER BY created_at")
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


# 1. Historical failed job + current run with zero jobs → NOT failed
def test_historical_failed_job_does_not_fail_zero_job_run(engine):
    from astock_api.run_lifecycle import create_run, start_run, set_executing, complete_run
    from astock_api.run_verifier import verify_run

    # Pre-existing failed job (from a previous cycle)
    _create_and_fail_job(engine, code="600001")

    # Current run: zero jobs created (dedup)
    run_id = create_run(engine, "2026-09-01")
    start_run(engine, run_id)
    set_executing(engine, run_id, jobs_created=0, job_ids=[])
    complete_run(engine, run_id, plan_hash="hash1", jobs_created=0, jobs_done=0, jobs_failed=0)

    result = verify_run(engine, run_id)
    assert result.status in ("PASS", "WARN"), (
        f"Expected PASS/WARN but got {result.status}: {result.errors}"
    )
    assert not any("SYSTEM_ERROR" in e for e in result.errors)


# 2. Previous cycle's failed jobs + second identical plan dedups to zero → NOT failed
def test_dedup_zero_job_run_not_failed_by_previous_jobs(engine):
    from astock_api.run_lifecycle import create_run, start_run, set_executing, complete_run
    from astock_api.run_verifier import verify_run

    # Cycle #1: creates 2 jobs that both fail
    run1 = create_run(engine, "2026-09-01")
    start_run(engine, run1)
    job_ids_1 = []
    for code in ["600002", "600003"]:
        engine.create_job("market_bars_update", _make_update_params(code))
    job_ids_1 = _get_job_ids(engine, limit=2)
    conn = engine._get_conn()
    try:
        placeholders = ",".join("?" * len(job_ids_1))
        conn.execute(
            f"UPDATE jobs SET status='FAILED', last_error='all sources unavailable' "
            f"WHERE job_id IN ({placeholders})",
            job_ids_1,
        )
        conn.commit()
    finally:
        conn.close()
    set_executing(engine, run1, jobs_created=2, job_ids=job_ids_1)
    complete_run(engine, run1, plan_hash="hash1", jobs_created=2, jobs_done=0, jobs_failed=2)

    # Cycle #2: same plan, dedup → zero new jobs
    run2 = create_run(engine, "2026-09-02")
    start_run(engine, run2)
    set_executing(engine, run2, jobs_created=0, job_ids=[])
    complete_run(engine, run2, plan_hash="hash1", jobs_created=0, jobs_done=0, jobs_failed=0)

    result = verify_run(engine, run2)
    assert result.status in ("PASS", "WARN"), (
        f"Cycle #2 should not be failed by cycle #1's jobs: {result.errors}"
    )
    assert not any("SYSTEM_ERROR" in e for e in result.errors)


# 3. Current run owns a failed job → correctly reports failure
def test_current_run_failed_job_reports_failure(engine):
    from astock_api.run_lifecycle import create_run, start_run, set_executing, complete_run
    from astock_api.run_verifier import verify_run

    run_id = create_run(engine, "2026-09-01")
    start_run(engine, run_id)

    engine.create_job("market_bars_update", _make_update_params("600004"))
    job_ids = _get_job_ids(engine, limit=1)

    conn = engine._get_conn()
    try:
        conn.execute(
            "UPDATE jobs SET status='FAILED', last_error='unexpected internal failure' "
            "WHERE job_id=?",
            job_ids,
        )
        conn.commit()
    finally:
        conn.close()

    set_executing(engine, run_id, jobs_created=1, job_ids=job_ids)
    complete_run(engine, run_id, plan_hash="hash1", jobs_created=1, jobs_done=0, jobs_failed=1)

    result = verify_run(engine, run_id)
    assert result.status == "FAIL"
    assert any("SYSTEM_ERROR" in e for e in result.errors)


# 4. Unrelated job created while run is active → does NOT contaminate
def test_unrelated_job_does_not_contaminate_run(engine):
    from astock_api.run_lifecycle import create_run, start_run, set_executing, complete_run
    from astock_api.run_verifier import verify_run

    # Run creates 1 successful job
    run_id = create_run(engine, "2026-09-01")
    start_run(engine, run_id)

    engine.create_job("market_bars_update", _make_update_params("600005"))
    run_job_ids = _get_job_ids(engine, limit=1)

    conn = engine._get_conn()
    try:
        conn.execute(
            "UPDATE jobs SET status='DONE', completed_chunks=1, failed_chunks=0 "
            "WHERE job_id=?",
            run_job_ids,
        )
        conn.commit()
    finally:
        conn.close()

    set_executing(engine, run_id, jobs_created=1, job_ids=run_job_ids)
    complete_run(engine, run_id, plan_hash="hash1", jobs_created=1, jobs_done=1, jobs_failed=0)

    # Unrelated manual job created AFTER the run (with a failure)
    engine.create_job("market_bars_update", _make_update_params("600006"))
    conn = engine._get_conn()
    try:
        conn.execute(
            "UPDATE jobs SET status='FAILED', last_error='unrelated system error' "
            "WHERE status='PENDING'"
        )
        conn.commit()
    finally:
        conn.close()

    result = verify_run(engine, run_id)
    assert result.status in ("PASS", "WARN"), (
        f"Unrelated job should not contaminate: {result.errors}"
    )
    assert not any("SYSTEM_ERROR" in e for e in result.errors)


# 5. Existing successful-run behavior remains correct
def test_successful_run_still_passes(engine):
    from astock_api.run_lifecycle import create_run, start_run, set_executing, complete_run
    from astock_api.run_verifier import verify_run

    run_id = create_run(engine, "2026-09-01")
    start_run(engine, run_id)

    job_ids = []
    for code in ["600519", "600520"]:
        engine.create_job("market_bars_update", _make_update_params(code))
    job_ids = _get_job_ids(engine, limit=2)

    conn = engine._get_conn()
    try:
        placeholders = ",".join("?" * len(job_ids))
        conn.execute(
            f"UPDATE jobs SET status='DONE', completed_chunks=1, failed_chunks=0 "
            f"WHERE job_id IN ({placeholders})",
            job_ids,
        )
        conn.commit()
    finally:
        conn.close()

    set_executing(engine, run_id, jobs_created=2, job_ids=job_ids)
    complete_run(engine, run_id, plan_hash="hash1", jobs_created=2, jobs_done=2, jobs_failed=0)

    result = verify_run(engine, run_id)
    assert result.status in ("PASS", "WARN")
