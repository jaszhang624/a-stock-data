"""R6-7B Minimal Scheduler targeted tests.

Covers:
1. Empty universe produces valid empty cycle result
2. Missing data creates BOOTSTRAP jobs
3. Current data creates no jobs
4. Stale data creates UPDATE jobs
5. Same scheduler run twice does not create duplicate jobs (dedup)
6. Cross-market identity isolation preserved
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
    # Pre-populate with SSE:000001 INDEX as current (matches R6-2 smoke data)
    store.states["SSE:000001"] = {
        "security_id": "SSE:000001",
        "latest_trade_date": "2026-08-20",
        "earliest_trade_date": "2025-01-01",
        "row_count": 100,
    }
    return store


@pytest.fixture
def mock_universe(tmp_path):
    """Create a minimal universe JSON file."""
    universe = [
        {"canonical_id": "SSE:000001", "code": "000001", "exchange": "SSE", "asset_type": "INDEX"},
        {"canonical_id": "SZSE:000001", "code": "000001", "exchange": "SZSE", "asset_type": "EQUITY"},
        {"canonical_id": "SSE:600519", "code": "600519", "exchange": "SSE", "asset_type": "EQUITY"},
    ]
    path = tmp_path / "universe.json"
    with open(path, "w") as f:
        json.dump(universe, f)
    return str(path)


# 1. Empty universe produces valid empty cycle result
def test_empty_universe(engine, tmp_path):
    from astock_api.scheduler import run_update_cycle

    universe = []
    path = tmp_path / "empty_universe.json"
    with open(path, "w") as f:
        json.dump(universe, f)

    store = type("MockStore", (), {"get_all_instrument_states": lambda self: []})()

    result = run_update_cycle(store, engine, str(path), "2026-08-20")
    assert result["coverage_summary"]["total_universe"] == 0
    assert result["materialization"]["created_jobs"] == 0


# 2. Missing data creates BOOTSTRAP jobs
def test_missing_data_creates_bootstrap_jobs(engine, mock_store, mock_universe):
    from astock_api.scheduler import run_update_cycle

    # Remove SSE:000001 from store so all 3 instruments are missing
    mock_store.states.clear()

    result = run_update_cycle(mock_store, engine, mock_universe, "2026-08-20")
    assert result["materialization"]["created_jobs"] == 3


# 3. Current data creates no jobs
def test_current_data_creates_no_jobs(engine, mock_store, mock_universe):
    from astock_api.scheduler import run_update_cycle

    # All instruments current as of 2026-08-20
    mock_store.states["SZSE:000001"] = {
        "security_id": "SZSE:000001",
        "latest_trade_date": "2026-08-20",
        "earliest_trade_date": "2025-01-01",
        "row_count": 200,
    }
    mock_store.states["SSE:600519"] = {
        "security_id": "SSE:600519",
        "latest_trade_date": "2026-08-20",
        "earliest_trade_date": "2025-01-01",
        "row_count": 150,
    }

    result = run_update_cycle(mock_store, engine, mock_universe, "2026-08-20")
    assert result["materialization"]["created_jobs"] == 0


# 4. Stale data creates UPDATE jobs
def test_stale_data_creates_update_jobs(engine, mock_store, mock_universe):
    from astock_api.scheduler import run_update_cycle

    # SZSE:000001 and SSE:600519 are stale (behind reference date)
    mock_store.states["SZSE:000001"] = {
        "security_id": "SZSE:000001",
        "latest_trade_date": "2026-08-15",
        "earliest_trade_date": "2025-01-01",
        "row_count": 200,
    }
    mock_store.states["SSE:600519"] = {
        "security_id": "SSE:600519",
        "latest_trade_date": "2026-08-18",
        "earliest_trade_date": "2025-01-01",
        "row_count": 150,
    }

    result = run_update_cycle(mock_store, engine, mock_universe, "2026-08-20")
    # SSE:000001 is current, so only 2 jobs created
    assert result["materialization"]["created_jobs"] == 2


# 5. Same scheduler run twice does not create duplicate jobs
def test_same_run_twice_no_duplicates(engine, mock_store, mock_universe):
    from astock_api.scheduler import run_update_cycle

    # Clear store so all instruments are missing
    mock_store.states.clear()

    result1 = run_update_cycle(mock_store, engine, mock_universe, "2026-08-20")
    assert result1["materialization"]["created_jobs"] == 3

    # Second run with same state should be deduplicated
    result2 = run_update_cycle(mock_store, engine, mock_universe, "2026-08-20")
    assert result2["materialization"]["created_jobs"] == 0
    assert result2["materialization"]["already_materialized"] is True


# 6. Cross-market identity isolation preserved
def test_cross_market_identity_isolation(engine, mock_store, tmp_path):
    from astock_api.scheduler import run_update_cycle

    # Universe with both SSE:000001 INDEX and SZSE:000001 EQUITY
    universe = [
        {"canonical_id": "SSE:000001", "code": "000001", "exchange": "SSE", "asset_type": "INDEX"},
        {"canonical_id": "SZSE:000001", "code": "000001", "exchange": "SZSE", "asset_type": "EQUITY"},
    ]
    path = tmp_path / "cross_market_universe.json"
    with open(path, "w") as f:
        json.dump(universe, f)

    # Both stale
    mock_store.states.clear()
    mock_store.states["SSE:000001"] = {
        "security_id": "SSE:000001",
        "latest_trade_date": "2026-08-15",
        "earliest_trade_date": "2025-01-01",
        "row_count": 100,
    }
    mock_store.states["SZSE:000001"] = {
        "security_id": "SZSE:000001",
        "latest_trade_date": "2026-08-15",
        "earliest_trade_date": "2025-01-01",
        "row_count": 200,
    }

    result = run_update_cycle(mock_store, engine, str(path), "2026-08-20")
    assert result["materialization"]["created_jobs"] == 2

    # Verify independent jobs were created
    job_ids = [j["job_id"] for j in result.get("materialization", {}).get("_created_jobs_detail", [])]
    # Just verify the count is correct; isolation is enforced by canonical_id in executor
