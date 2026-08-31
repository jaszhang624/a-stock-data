"""R7-1 Run Lifecycle targeted tests.

Covers:
1. Successful run lifecycle: CREATED → RUNNING → PLANNED → EXECUTING → SUCCESS
2. Failed run lifecycle: CREATED → RUNNING → FAILED with error_message
3. Interrupted recovery: stale in-progress run marked INTERRUPTED on init
4. Restart persistence: state survives new engine instance
5. Cross-run ordering: multiple runs, latest selection works correctly
6. Health endpoint: /health/run returns correct state
"""

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


# 1. Successful run lifecycle: CREATED → RUNNING → PLANNED → EXECUTING → SUCCESS
def test_successful_run_lifecycle(engine):
    from astock_api.run_lifecycle import (
        initialize_run_lifecycle, create_run, start_run, set_planned,
        set_executing, complete_run, get_run,
    )

    initialize_run_lifecycle(engine)

    run_id = create_run(engine, "2026-08-20")
    run = get_run(engine, run_id)
    assert run["status"] == "CREATED"

    start_run(engine, run_id)
    assert get_run(engine, run_id)["status"] == "RUNNING"

    set_planned(engine, run_id, plan_hash="abc123")
    assert get_run(engine, run_id)["status"] == "PLANNED"

    set_executing(engine, run_id, jobs_created=5)
    assert get_run(engine, run_id)["status"] == "EXECUTING"

    complete_run(engine, run_id, plan_hash="abc123", jobs_created=5, jobs_done=4, jobs_failed=0)
    run = get_run(engine, run_id)

    assert run["status"] == "SUCCESS"
    assert run["plan_hash"] == "abc123"
    assert run["jobs_created"] == 5
    assert run["jobs_done"] == 4
    assert run["jobs_failed"] == 0
    assert run["finished_at"] is not None


# 2. Failed run lifecycle: CREATED → RUNNING → FAILED with error_message
def test_failed_run_lifecycle(engine):
    from astock_api.run_lifecycle import (
        initialize_run_lifecycle, create_run, start_run, fail_run, get_run,
    )

    initialize_run_lifecycle(engine)

    run_id = create_run(engine, "2026-08-20")
    start_run(engine, run_id)

    fail_run(engine, run_id, error_message="Planner failed: no coverage data")
    run = get_run(engine, run_id)

    assert run["status"] == "FAILED"
    assert run["error_message"] == "Planner failed: no coverage data"
    assert run["finished_at"] is not None


# 3. Interrupted recovery: stale in-progress run marked INTERRUPTED on init
def test_interrupted_recovery(engine, tmp_path):
    from astock_api.run_lifecycle import (
        initialize_run_lifecycle, create_run, start_run, set_planned, get_run,
    )

    initialize_run_lifecycle(engine)

    # Simulate a run that was interrupted during PLANNED phase
    run_id = create_run(engine, "2026-08-20")
    start_run(engine, run_id)
    set_planned(engine, run_id, plan_hash="partial")

    assert get_run(engine, run_id)["status"] == "PLANNED"

    # Create new engine instance pointing to same DB (simulates restart)
    import sqlite3
    engine2 = type("JobEngine", (), {"db_path": engine.db_path, "data_dir": str(tmp_path)})
    engine2._get_conn = lambda: sqlite3.connect(engine.db_path, check_same_thread=False)
    initialize_run_lifecycle(engine2)

    run = get_run(engine2, run_id)
    assert run["status"] == "INTERRUPTED"


# 4. Restart persistence: state survives new engine instance
def test_restart_persistence(engine, tmp_path):
    from astock_api.run_lifecycle import (
        initialize_run_lifecycle, create_run, start_run, complete_run, get_latest_run,
    )

    initialize_run_lifecycle(engine)

    run_id = create_run(engine, "2026-08-20")
    start_run(engine, run_id)
    complete_run(engine, run_id, plan_hash="xyz789", jobs_created=10)

    # Create new engine instance pointing to same DB
    import sqlite3
    engine2 = type("JobEngine", (), {"db_path": engine.db_path, "data_dir": str(tmp_path)})
    engine2._get_conn = lambda: sqlite3.connect(engine.db_path, check_same_thread=False)
    latest = get_latest_run(engine2)
    assert latest is not None
    assert latest["status"] == "SUCCESS"
    assert latest["plan_hash"] == "xyz789"
    assert latest["jobs_created"] == 10


# 5. Cross-run ordering: multiple runs, latest selection works correctly
def test_cross_run_ordering(engine):
    from astock_api.run_lifecycle import (
        initialize_run_lifecycle, create_run, start_run, complete_run, fail_run, get_latest_run, get_runs,
    )

    initialize_run_lifecycle(engine)

    # Run 1: SUCCESS
    r1 = create_run(engine, "2026-08-19")
    start_run(engine, r1)
    complete_run(engine, r1, plan_hash="hash1", jobs_created=5)

    # Run 2: FAILED
    r2 = create_run(engine, "2026-08-20")
    start_run(engine, r2)
    fail_run(engine, r2, error_message="executor timeout")

    # Run 3: SUCCESS
    r3 = create_run(engine, "2026-08-21")
    start_run(engine, r3)
    complete_run(engine, r3, plan_hash="hash3", jobs_created=8)

    # Latest should be run 3
    latest = get_latest_run(engine)
    assert latest["run_id"] == r3
    assert latest["status"] == "SUCCESS"

    # get_runs returns all 3, most recent first
    runs = get_runs(engine)
    assert len(runs) == 3
    assert runs[0]["run_id"] == r3
    assert runs[1]["run_id"] == r2
    assert runs[2]["run_id"] == r1


# 6. Health endpoint: /health/run returns correct state
def test_health_run_endpoint(engine):
    from astock_api.run_lifecycle import (
        initialize_run_lifecycle, create_run, start_run, complete_run, get_latest_run,
    )

    initialize_run_lifecycle(engine)

    # Before any runs
    latest = get_latest_run(engine)
    assert latest is None

    # After a successful run
    run_id = create_run(engine, "2026-08-20")
    start_run(engine, run_id)
    complete_run(engine, run_id, plan_hash="health_test", jobs_created=3, quality_status="PASS")

    latest = get_latest_run(engine)
    assert latest["run_id"] == run_id
    assert latest["status"] == "SUCCESS"
    assert latest["reference_date"] == "2026-08-20"
    assert latest["quality_status"] == "PASS"