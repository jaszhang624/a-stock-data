"""R6-8A Daily Operation Report targeted tests.

Covers:
1. Empty system — missing data, no jobs, no scheduler history
2. Existing data — coverage counts reflect actual state
3. Scheduler success — last scheduler state appears in report
4. Scheduler failure — FAILED state appears in report
5. Cross-market isolation — SSE:000001 INDEX and SZSE:000001 EQUITY remain separate
6. Deterministic output — same DB + same reference_date produces same report (except generated_at)
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
        {"canonical_id": "SZSE:000001", "code": "000001", "exchange": "SZSE", "asset_type": "EQUITY"},
    ]
    path = tmp_path / "universe.json"
    with open(path, "w") as f:
        json.dump(universe, f)
    return str(path)


# 1. Empty system: missing data, no jobs, no scheduler history
def test_empty_system_report(engine, mock_store, mock_universe):
    from astock_api.operation_report import generate_daily_report

    # No data in store, no jobs created
    report = generate_daily_report(mock_store, engine, mock_universe, "2026-08-20")

    assert report["reference_date"] == "2026-08-20"
    assert report["universe"]["total"] == 2
    assert report["coverage"]["missing"] == 2
    assert report["freshness"]["MISSING"] == 2
    # No scheduler history yet
    assert report["scheduler"]["last_status"] is None
    # No jobs created
    assert report["jobs"]["pending"] == 0


# 2. Existing data: coverage counts reflect actual state
def test_existing_data_coverage(engine, mock_store, mock_universe):
    from astock_api.operation_report import generate_daily_report

    # Add data for one instrument (current as of reference date)
    mock_store.states["SSE:600519"] = {
        "security_id": "SSE:600519",
        "latest_trade_date": "2026-08-20",
        "earliest_trade_date": "2025-01-01",
        "row_count": 500,
    }

    report = generate_daily_report(mock_store, engine, mock_universe, "2026-08-20")

    assert report["coverage"]["with_data"] == 1
    assert report["coverage"]["current"] == 1
    assert report["coverage"]["missing"] == 1
    assert report["freshness"]["CURRENT"] == 1
    assert report["freshness"]["MISSING"] == 1


# 3. Scheduler success: last scheduler state appears in report
def test_scheduler_success_in_report(engine, mock_store, mock_universe):
    from astock_api.operation_report import generate_daily_report
    from astock_api.scheduler_state import initialize_scheduler_state, start_run, complete_run

    # Simulate a successful scheduler run
    initialize_scheduler_state(engine)
    run_id = start_run(engine, "update_cycle")
    complete_run(engine, run_id, plan_hash="test-hash-123", created_jobs=42)

    report = generate_daily_report(mock_store, engine, mock_universe, "2026-08-20")

    assert report["scheduler"]["last_status"] == "SUCCESS"
    assert report["scheduler"]["last_plan_hash"] == "test-hash-123"
    assert report["scheduler"]["jobs_created"] == 42


# 4. Scheduler failure: FAILED state appears in report
def test_scheduler_failure_in_report(engine, mock_store, mock_universe):
    from astock_api.operation_report import generate_daily_report
    from astock_api.scheduler_state import initialize_scheduler_state, start_run, fail_run

    # Simulate a failed scheduler run
    initialize_scheduler_state(engine)
    run_id = start_run(engine, "update_cycle")
    fail_run(engine, run_id, error_message="coverage generation failed")

    report = generate_daily_report(mock_store, engine, mock_universe, "2026-08-20")

    assert report["scheduler"]["last_status"] == "FAILED"


# 5. Cross-market isolation: SSE:000001 INDEX and SZSE:000001 EQUITY remain separate
def test_cross_market_isolation(engine, tmp_path):
    from astock_api.operation_report import generate_daily_report

    # Universe with both INDEX and EQUITY sharing code 000001
    universe = [
        {"canonical_id": "SSE:000001", "code": "000001", "exchange": "SSE", "asset_type": "INDEX"},
        {"canonical_id": "SZSE:000001", "code": "000001", "exchange": "SZSE", "asset_type": "EQUITY"},
    ]
    path = tmp_path / "cross_market_universe.json"
    with open(path, "w") as f:
        json.dump(universe, f)

    class MockStore:
        def get_all_instrument_states(self):
            return [
                {
                    "security_id": "SSE:000001",
                    "latest_trade_date": "2026-08-20",
                    "earliest_trade_date": "2025-01-01",
                    "row_count": 100,
                },
                {
                    "security_id": "SZSE:000001",
                    "latest_trade_date": "2026-08-15",
                    "earliest_trade_date": "2025-01-01",
                    "row_count": 200,
                },
            ]

    report = generate_daily_report(MockStore(), engine, str(path), "2026-08-20")

    # Both instruments should be counted independently
    assert report["universe"]["total"] == 2
    assert report["coverage"]["with_data"] == 2
    # SSE:000001 is CURRENT, SZSE:000001 is STALE
    assert report["freshness"]["CURRENT"] == 1
    assert report["freshness"]["STALE"] == 1


# 6. Deterministic output: same DB + same reference_date → same report (except generated_at)
def test_deterministic_output(engine, mock_store, mock_universe):
    from astock_api.operation_report import generate_daily_report

    # Add deterministic data
    mock_store.states["SSE:600519"] = {
        "security_id": "SSE:600519",
        "latest_trade_date": "2026-08-20",
        "earliest_trade_date": "2025-01-01",
        "row_count": 500,
    }

    report1 = generate_daily_report(mock_store, engine, mock_universe, "2026-08-20")
    report2 = generate_daily_report(mock_store, engine, mock_universe, "2026-08-20")

    # Remove generated_at for comparison
    report1.pop("generated_at")
    report2.pop("generated_at")

    assert report1 == report2
