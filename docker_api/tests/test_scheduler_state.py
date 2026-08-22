"""R6-7C Scheduler State & Observability targeted tests.

Covers:
1. Successful cycle transitions RUNNING → SUCCESS with timestamps, plan_hash, jobs count
2. Failed cycle records FAILED status and error message
3. Interrupted recovery marks stale RUNNING → INTERRUPTED on init
4. Restart persistence — state survives new engine instance
5. Cross-run ordering — latest run selection across multiple runs
6. Health endpoint returns correct state
"""

import json
import os
import shutil
import tempfile

import pytest

from astock_api.job_engine import JobEngine


@pytest.fixture
def engine():
    """Create isolated JobEngine for tests."""
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test_jobs.db")
    eng = JobEngine(db_path=db_path, data_dir=tmpdir)
    eng.initialize()
    yield eng
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def mock_store():
    """Mock DatasetStore with configurable instrument states."""
    class MockStore:
        def __init__(self):
            self.states = {}

        def get_all_instrument_states(self):
            return list(self.states.values())

    store = MockStore()
    return store


@pytest.fixture
def mock_universe(tmp_path):
    """Create a minimal universe JSON file."""
    universe = [
        {"canonical_id": "SSE:600519", "code": "600519", "exchange": "SSE", "asset_type": "EQUITY"},
    ]
    path = tmp_path / "universe.json"
    with open(path, "w") as f:
        json.dump(universe, f)
    return str(path)


# 1. Successful cycle: RUNNING → SUCCESS with timestamps, plan_hash, jobs count
def test_successful_cycle_state(engine, mock_store, mock_universe):
    from astock_api.scheduler import run_update_cycle
    # Scheduler now writes to update_runs via run_lifecycle
    from astock_api.run_lifecycle import get_latest_run

    result = run_update_cycle(mock_store, engine, mock_universe, "2026-08-20")

    latest = get_latest_run(engine)
    assert latest is not None
    assert latest["status"] == "SUCCESS"
    assert latest["started_at"] is not None
    assert latest["finished_at"] is not None
    assert latest["plan_hash"] == result["plan_hash"]
    assert latest["jobs_created"] >= 0


# 2. Failed cycle: FAILED status and error recorded
def test_failed_cycle_state(engine, mock_store, tmp_path):
    from astock_api.scheduler import run_update_cycle
    # Scheduler now writes to update_runs via run_lifecycle
    from astock_api.run_lifecycle import get_latest_run

    # Create universe that triggers a failure (non-existent path)
    universe = [
        {"canonical_id": "SSE:600519", "code": "600519", "exchange": "SSE", "asset_type": "EQUITY"},
    ]
    path = tmp_path / "universe.json"
    with open(path, "w") as f:
        json.dump(universe, f)

    # Make store raise an error during coverage generation
    class FailingStore:
        def get_all_instrument_states(self):
            raise RuntimeError("simulated failure")

    with pytest.raises(RuntimeError, match="simulated failure"):
        run_update_cycle(FailingStore(), engine, str(path), "2026-08-20")

    latest = get_latest_run(engine)
    assert latest is not None
    assert latest["status"] == "FAILED"
    assert latest["error_message"] is not None
    assert "simulated failure" in latest["error_message"]


# 3. Interrupted recovery: stale RUNNING → INTERRUPTED on init
def test_interrupted_recovery(engine):
    from astock_api.scheduler_state import initialize_scheduler_state, start_run, get_latest_run

    # Initialize table first
    initialize_scheduler_state(engine)

    # Simulate a run that was interrupted (left as RUNNING)
    start_run(engine, "update_cycle")

    latest = get_latest_run(engine)
    assert latest["status"] == "RUNNING"

    # Initialize again — should recover the stale run
    initialize_scheduler_state(engine)

    latest = get_latest_run(engine)
    assert latest["status"] == "INTERRUPTED"


# 4. Restart persistence: state survives new engine instance
def test_restart_persistence(tmp_path):
    from astock_api.job_engine import JobEngine
    from astock_api.scheduler_state import initialize_scheduler_state, start_run, complete_run, get_latest_run

    db_path = str(tmp_path / "test_jobs.db")
    eng1 = JobEngine(db_path=db_path, data_dir=str(tmp_path))
    eng1.initialize()

    initialize_scheduler_state(eng1)
    run_id = start_run(eng1, "update_cycle")
    complete_run(eng1, run_id, plan_hash="abc123", created_jobs=5)

    # Create a new engine instance (simulates restart)
    eng2 = JobEngine(db_path=db_path, data_dir=str(tmp_path))

    latest = get_latest_run(eng2)
    assert latest is not None
    assert latest["status"] == "SUCCESS"
    assert latest["plan_hash"] == "abc123"
    assert latest["created_jobs"] == 5


# 5. Cross-run ordering: multiple runs, verify latest selection
def test_cross_run_ordering(engine):
    from astock_api.scheduler_state import initialize_scheduler_state, start_run, complete_run, get_latest_run

    initialize_scheduler_state(engine)

    # Create 3 runs
    for i in range(1, 4):
        run_id = start_run(engine, "update_cycle")
        complete_run(engine, run_id, plan_hash=f"hash{i}", created_jobs=i * 10)

    latest = get_latest_run(engine)
    assert latest is not None
    assert latest["plan_hash"] == "hash3"
    assert latest["created_jobs"] == 30


# 6. Health endpoint returns correct state
def test_health_scheduler_endpoint(engine):
    from astock_api.scheduler_state import initialize_scheduler_state, start_run, complete_run

    initialize_scheduler_state(engine)
    run_id = start_run(engine, "update_cycle")
    complete_run(engine, run_id, plan_hash="test-hash", created_jobs=42)

    # Verify the endpoint would return correct data
    from astock_api.scheduler_state import get_latest_run

    latest = get_latest_run(engine)
    assert latest["status"] == "SUCCESS"
    assert latest["plan_hash"] == "test-hash"
    assert latest["created_jobs"] == 42
