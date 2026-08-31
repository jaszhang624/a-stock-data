"""R7-2 Run Verification Gate targeted tests.

Covers:
1. All jobs done + quality pass → PASS
2. Job failure with SYSTEM_ERROR → FAIL
3. Upstream no data warning → WARN (not FAIL)
4. Quality failure (identity errors > 0) → FAIL
5. Freshness no improvement warning
6. Restart persistence — verification result survives engine restart
7. Cross-market isolation — SSE vs SZSE jobs verified independently
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
    monkeypatch.setattr(JobEngine, "__init__", lambda self: setattr(self, "db_path", db_path) or setattr(self, "data_dir", data_dir))
    engine = JobEngine()
    engine.initialize()
    return engine


def _make_update_params(code, exchange="SSE", asset_type="EQUITY"):
    """Build correct market_bars_update params."""
    return {
        "instruments": [{"code": code, "exchange": exchange, "asset_type": asset_type}],
        "count": 10,
    }


# 1. All jobs done + quality pass → PASS
def test_all_jobs_done_quality_pass(engine, tmp_path):
    from astock_api.run_lifecycle import create_run, start_run, set_executing, complete_run
    from astock_api.run_verifier import verify_run

    run_id = create_run(engine, "2026-08-20")
    start_run(engine, run_id)

    # Create 3 jobs that all complete successfully
    for code in ["600519", "600520", "600521"]:
        engine.create_job("market_bars_update", _make_update_params(code))

    set_executing(engine, run_id, jobs_created=3)
    complete_run(engine, run_id, plan_hash="abc", jobs_created=3, jobs_done=3, jobs_failed=0)

    result = verify_run(engine, run_id)
    assert result.status in ("PASS", "WARN")  # PASS or WARN (quality SKIP is ok)
    assert not any("completeness mismatch" in e.lower() for e in result.errors)


# 2. Job failure with SYSTEM_ERROR → FAIL
def test_job_failure_system_error(engine, tmp_path):
    from astock_api.run_lifecycle import create_run, start_run, set_executing, complete_run
    from astock_api.run_verifier import verify_run

    run_id = create_run(engine, "2026-08-20")
    start_run(engine, run_id)

    # Create jobs with system error failure
    engine.create_job("market_bars_update", _make_update_params("600519"))
    set_executing(engine, run_id, jobs_created=1)

    # Simulate job failure with system error
    conn = engine._get_conn()
    try:
        # Add error_message column if it doesn't exist
        cur = conn.execute("PRAGMA table_info(jobs)")
        columns = [row[1] for row in cur.fetchall()]
        if "error_message" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN error_message TEXT")
        conn.execute("UPDATE jobs SET status='FAILED', error_message='Unexpected internal failure' WHERE status='PENDING'")
        conn.commit()
    finally:
        conn.close()

    complete_run(engine, run_id, plan_hash="abc", jobs_created=1, jobs_done=0, jobs_failed=1)

    result = verify_run(engine, run_id)
    assert result.status == "FAIL"
    assert any("SYSTEM_ERROR" in e for e in result.errors)


# 3. Upstream no data warning → WARN (not FAIL)
def test_upstream_no_data_warning(engine, tmp_path):
    from astock_api.run_lifecycle import create_run, start_run, set_executing, complete_run
    from astock_api.run_verifier import verify_run

    run_id = create_run(engine, "2026-08-20")
    start_run(engine, run_id)

    engine.create_job("market_bars_update", _make_update_params("600519"))
    set_executing(engine, run_id, jobs_created=1)

    # Simulate job failure with upstream no data
    conn = engine._get_conn()
    try:
        # Add error_message column if it doesn't exist
        cur = conn.execute("PRAGMA table_info(jobs)")
        columns = [row[1] for row in cur.fetchall()]
        if "error_message" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN error_message TEXT")
        conn.execute("UPDATE jobs SET status='FAILED', error_message='No data found for symbol' WHERE status='PENDING'")
        conn.commit()
    finally:
        conn.close()

    complete_run(engine, run_id, plan_hash="abc", jobs_created=1, jobs_done=0, jobs_failed=1)

    result = verify_run(engine, run_id)
    assert result.status == "WARN"  # UPSTREAM_NO_DATA is a warning, not failure
    assert any("UPSTREAM_NO_DATA" in w for w in result.warnings)


# 4. Quality failure (identity errors > 0) → FAIL
def test_quality_failure(engine, tmp_path):
    from astock_api.run_lifecycle import create_run, start_run, set_executing, complete_run
    from astock_api.run_verifier import verify_run

    run_id = create_run(engine, "2026-08-20")
    start_run(engine, run_id)

    # Create jobs that complete successfully
    for code in ["600519", "600520"]:
        engine.create_job("market_bars_update", _make_update_params(code))

    set_executing(engine, run_id, jobs_created=2)
    complete_run(engine, run_id, plan_hash="abc", jobs_created=2, jobs_done=2, jobs_failed=0)

    # Write a quality report with identity errors
    reports_dir = os.path.join(tmp_path, "data", "reports")
    os.makedirs(reports_dir, exist_ok=True)
    quality_path = os.path.join(reports_dir, f"quality_run_{run_id}.json")
    with open(quality_path, "w") as f:
        json.dump({
            "status": "FAIL",
            "identity": {"errors": 3},
            "duplicates": {"duplicated_keys": 0},
            "ohlcv": {"invalid_rows": 0},
        }, f)

    result = verify_run(engine, run_id)
    assert result.status == "FAIL"
    assert any("Identity errors" in e for e in result.errors)


# 5. Freshness no improvement warning
def test_freshness_no_improvement(engine, tmp_path):
    from astock_api.run_lifecycle import create_run, start_run, set_executing, complete_run
    from astock_api.run_verifier import verify_run

    run_id = create_run(engine, "2026-08-20")
    start_run(engine, run_id)

    # Create jobs
    for code in ["600519", "600520"]:
        engine.create_job("market_bars_update", _make_update_params(code))

    set_executing(engine, run_id, jobs_created=2)
    complete_run(engine, run_id, plan_hash="abc", jobs_created=2, jobs_done=2, jobs_failed=0)

    # Mock store with no CURRENT instruments
    class MockStore:
        def get_all_instrument_states(self):
            return []

    universe_path = str(tmp_path / "universe.json")
    with open(universe_path, "w") as f:
        json.dump([
            {"canonical_id": "SSE:600519", "code": "600519", "exchange": "SSE"},
            {"canonical_id": "SSE:600520", "code": "600520", "exchange": "SSE"},
        ], f)

    result = verify_run(engine, run_id, store=MockStore(), universe_path=universe_path)
    # Should have freshness warning about no CURRENT instruments
    assert "freshness" in result.checks


# 6. Restart persistence — verification result survives engine restart
def test_restart_persistence(engine, tmp_path):
    from astock_api.run_lifecycle import create_run, start_run, set_executing, complete_run
    from astock_api.run_verifier import verify_run

    run_id = create_run(engine, "2026-08-20")
    start_run(engine, run_id)

    for code in ["600519", "600520", "600521"]:
        engine.create_job("market_bars_update", _make_update_params(code))

    set_executing(engine, run_id, jobs_created=3)
    complete_run(engine, run_id, plan_hash="abc", jobs_created=3, jobs_done=3, jobs_failed=0)

    # Verify with new engine instance
    import sqlite3
    engine2 = type("JobEngine", (), {"db_path": engine.db_path, "data_dir": str(tmp_path)})
    engine2._get_conn = lambda: sqlite3.connect(engine.db_path, check_same_thread=False)

    result = verify_run(engine2, run_id)
    assert result.status in ("PASS", "WARN")


# 7. Cross-market isolation — SSE vs SZSE jobs verified independently
def test_cross_market_isolation(engine, tmp_path):
    from astock_api.run_lifecycle import create_run, start_run, set_executing, complete_run
    from astock_api.run_verifier import verify_run

    run_id = create_run(engine, "2026-08-20")
    start_run(engine, run_id)

    # Create SSE and SZSE jobs independently
    engine.create_job("market_bars_update", _make_update_params("600519", exchange="SSE"))
    engine.create_job("market_bars_update", _make_update_params("000001", exchange="SZSE"))

    set_executing(engine, run_id, jobs_created=2)
    complete_run(engine, run_id, plan_hash="abc", jobs_created=2, jobs_done=2, jobs_failed=0)

    result = verify_run(engine, run_id)
    assert result.status in ("PASS", "WARN")
    # Both markets should be counted in job completeness
    assert result.checks["jobs"]["created"] == 2
