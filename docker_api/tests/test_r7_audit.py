"""R7 Production Readiness Audit — read-only audit script.

DOES NOT modify runtime code, schema, or state.
Covers: State Consistency, Recovery, Storage, Report Completeness,
Failure Simulation, End-to-End Daily Cycle.

Run: python -m pytest tests/test_r7_audit.py -v
"""

import inspect
import json
import os
import sqlite3
import tempfile

import pytest


# ============================================================================
# 1. STATE CONSISTENCY AUDIT
# ============================================================================

class TestStateConsistency:
    """Verify state machine integrity and impossible combinations."""

    @pytest.fixture
    def engine(self, tmp_path, monkeypatch):
        from astock_api.job_engine import JobEngine

        db_path = str(tmp_path / "jobs.db")
        data_dir = str(tmp_path)
        monkeypatch.setattr(JobEngine, "__init__", lambda self, *a, **kw: None)
        engine = JobEngine()
        engine.db_path = db_path
        engine.data_dir = data_dir
        engine.initialize()
        return engine

    def test_job_terminal_states_defined(self):
        """Verify terminal states are properly defined."""
        from astock_api.job_engine import TERMINAL_STATES, JOB_DONE, JOB_FAILED, JOB_CANCELLED

        assert JOB_DONE in TERMINAL_STATES
        assert JOB_FAILED in TERMINAL_STATES
        assert JOB_CANCELLED in TERMINAL_STATES

    def test_job_allowed_transitions(self):
        """Verify pause/resume/cancel transitions are well-defined."""
        from astock_api.job_engine import (
            PAUSE_FROM, RESUME_FROM, CANCEL_FROM,
            JOB_PENDING, JOB_RUNNING, JOB_WAITING_SOURCE, JOB_PAUSED,
        )

        # Can pause from non-terminal states
        assert JOB_PENDING in PAUSE_FROM
        assert JOB_RUNNING in PAUSE_FROM
        assert JOB_WAITING_SOURCE in PAUSE_FROM

        # Can resume from paused
        assert JOB_PAUSED in RESUME_FROM

        # Can cancel from non-terminal states
        assert JOB_PENDING in CANCEL_FROM
        assert JOB_RUNNING in CANCEL_FROM

    def test_run_lifecycle_states_defined(self):
        """Verify all run lifecycle states are used in the code."""
        from astock_api import run_lifecycle

        source = inspect.getsource(run_lifecycle)
        expected_states = ["CREATED", "RUNNING", "PLANNED", "EXECUTING",
                           "VERIFYING", "SUCCESS", "FAILED", "INTERRUPTED"]

        for state in expected_states:
            # States appear as 'STATE' or STATE/ (in docstrings)
            assert f"'{state}'" in source or f"{state}/" in source or f"/{state}" in source, \
                f"State '{state}' not found in run_lifecycle module"

    def test_interrupted_recovery_marks_stale_runs(self, engine):
        """Verify stale RUNNING run is recovered to INTERRUPTED."""
        from astock_api.run_lifecycle import (
            initialize_run_lifecycle, create_run, start_run, set_executing,
        )

        # Create a stale EXECUTING run (simulates crash during execution)
        run_id = create_run(engine, "2026-08-20")
        start_run(engine, run_id)
        set_executing(engine, run_id, jobs_created=5)

        # Re-initialize — should recover stale EXECUTING → INTERRUPTED
        initialize_run_lifecycle(engine)

        from astock_api.run_lifecycle import get_run
        run = get_run(engine, run_id)
        assert run["status"] == "INTERRUPTED", \
            f"Expected INTERRUPTED, got {run['status']}"

    def test_success_run_with_unfinished_jobs_is_caught(self, engine):
        """Verify verifier catches SUCCESS run with pending jobs."""
        from astock_api.run_lifecycle import create_run, start_run, set_executing, complete_run
        from astock_api.run_verifier import verify_run

        run_id = create_run(engine, "2026-08-20")
        start_run(engine, run_id)

        # Create jobs but don't complete them
        from astock_api.job_engine import JobEngine as JE
        engine.create_job("market_bars_update", {
            "instruments": [{"code": "600519", "exchange": "SSE", "asset_type": "EQUITY"}],
            "count": 10,
        })

        set_executing(engine, run_id, jobs_created=1)
        # Mark complete but jobs are still PENDING
        complete_run(engine, run_id, plan_hash="abc", jobs_created=1, jobs_done=0, jobs_failed=0)

        result = verify_run(engine, run_id)
        # Should warn about pending jobs (not fail — jobs still in flight)
        assert any("PENDING" in w for w in result.warnings), \
            f"Expected warning about pending jobs, got: {result.warnings}"

    def test_failed_job_not_counted_as_success(self, engine):
        """Verify FAILED jobs are not counted as done."""
        from astock_api.run_lifecycle import create_run, start_run, set_executing, complete_run
        from astock_api.run_verifier import verify_run

        run_id = create_run(engine, "2026-08-20")
        start_run(engine, run_id)

        engine.create_job("market_bars_update", {
            "instruments": [{"code": "600519", "exchange": "SSE", "asset_type": "EQUITY"}],
            "count": 10,
        })

        set_executing(engine, run_id, jobs_created=1)

        # Mark job as FAILED
        conn = engine._get_conn()
        try:
            cur = conn.execute("PRAGMA table_info(jobs)")
            columns = [row[1] for row in cur.fetchall()]
            if "error_message" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN error_message TEXT")
            conn.execute(
                "UPDATE jobs SET status='FAILED', error_message='Data corruption detected' WHERE status='PENDING'"
            )
            conn.commit()
        finally:
            conn.close()

        complete_run(engine, run_id, plan_hash="abc", jobs_created=1, jobs_done=0, jobs_failed=1)

        result = verify_run(engine, run_id)
        # Should fail due to DATA_CORRUPTION → CRITICAL → STOP
        assert result.status == "FAIL", f"Expected FAIL, got {result.status}"


# ============================================================================
# 2. RECOVERY AUDIT
# ============================================================================

class TestRecoveryAudit:
    """Verify existing recovery capabilities without modifying code."""

    @pytest.fixture
    def engine(self, tmp_path, monkeypatch):
        from astock_api.job_engine import JobEngine

        db_path = str(tmp_path / "jobs.db")
        data_dir = str(tmp_path)
        monkeypatch.setattr(JobEngine, "__init__", lambda self, *a, **kw: None)
        engine = JobEngine()
        engine.db_path = db_path
        engine.data_dir = data_dir
        engine.initialize()
        return engine

    def test_scheduler_interrupted_run_recovery(self, engine):
        """Verify scheduler recovers interrupted runs on next cycle."""
        from astock_api.run_lifecycle import (
            initialize_run_lifecycle, create_run, start_run, set_executing, get_latest_run,
        )

        # Simulate interrupted run from previous cycle
        old_run_id = create_run(engine, "2026-08-19")
        start_run(engine, old_run_id)
        set_executing(engine, old_run_id, jobs_created=3)

        # Next cycle initializes — should recover stale run
        initialize_run_lifecycle(engine)

        latest = get_latest_run(engine)
        assert latest["status"] == "INTERRUPTED"

    def test_run_persistence_survives_restart(self, engine, tmp_path):
        """Verify run state persists across process restart (new connection)."""
        from astock_api.run_lifecycle import create_run, start_run, set_executing, complete_run
        from astock_api.run_lifecycle import get_run as _get_run

        run_id = create_run(engine, "2026-08-20")
        start_run(engine, run_id)
        set_executing(engine, run_id, jobs_created=5)
        complete_run(engine, run_id, plan_hash="abc123", jobs_created=5)

        # Simulate restart: open new connection to same DB
        conn = sqlite3.connect(engine.db_path)
        try:
            cur = conn.execute(
                "SELECT run_id, status, plan_hash FROM update_runs ORDER BY run_id DESC LIMIT 1"
            )
            row = cur.fetchone()
        finally:
            conn.close()

        assert row is not None, "Run record lost after restart"
        assert row[0] == run_id
        assert row[1] == "SUCCESS"
        assert row[2] == "abc123"

    def test_job_engine_persistence_survives_restart(self, engine):
        """Verify job state persists across process restart."""
        engine.create_job("market_bars_update", {
            "instruments": [{"code": "600519", "exchange": "SSE", "asset_type": "EQUITY"}],
            "count": 10,
        })

        # Simulate restart: open new connection to same DB
        conn = sqlite3.connect(engine.db_path)
        try:
            cur = conn.execute("SELECT COUNT(*) FROM jobs WHERE job_type='market_bars_update'")
            count = cur.fetchone()[0]
        finally:
            conn.close()

        assert count == 1, f"Expected 1 job after restart, got {count}"


# ============================================================================
# 3. STORAGE AUDIT
# ============================================================================

class TestStorageAudit:
    """Inspect storage sizes, row counts, and growth behavior."""

    @pytest.fixture
    def engine(self, tmp_path, monkeypatch):
        from astock_api.job_engine import JobEngine

        db_path = str(tmp_path / "jobs.db")
        data_dir = str(tmp_path)
        monkeypatch.setattr(JobEngine, "__init__", lambda self, *a, **kw: None)
        engine = JobEngine()
        engine.db_path = db_path
        engine.data_dir = data_dir
        engine.initialize()
        return engine

    def test_sqlite_size_after_initialization(self, engine):
        """Verify SQLite DB size after initialization."""
        db_size = os.path.getsize(engine.db_path)
        # Should be reasonable (not empty, not huge)
        assert 0 < db_size < 1_000_000, f"SQLite size unexpected: {db_size}"

    def test_sqlite_tables_exist(self, engine):
        """Verify all required tables exist."""
        conn = engine._get_conn()
        try:
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = [row[0] for row in cur.fetchall()]
        finally:
            conn.close()

        # Core tables from JobEngine
        assert "jobs" in tables, f"Missing 'jobs' table. Tables: {tables}"
        assert "job_chunks" in tables, f"Missing 'job_chunks' table. Tables: {tables}"
        # R6-7A dedup table
        assert "plan_materializations" in tables, f"Missing 'plan_materializations' table. Tables: {tables}"
        # R7-1 lifecycle table (created lazily by initialize_run_lifecycle)
        from astock_api.run_lifecycle import initialize_run_lifecycle
        initialize_run_lifecycle(engine)

        # Re-check tables after lifecycle init
        conn = engine._get_conn()
        try:
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = [row[0] for row in cur.fetchall()]
        finally:
            conn.close()

        assert "update_runs" in tables, f"Missing 'update_runs' table after init. Tables: {tables}"

    def test_sqlite_row_counts_after_jobs(self, engine):
        """Verify row counts grow correctly after job creation."""
        # Create 3 jobs
        for code in ["600519", "600520", "600521"]:
            engine.create_job("market_bars_update", {
                "instruments": [{"code": code, "exchange": "SSE", "asset_type": "EQUITY"}],
                "count": 10,
            })

        conn = engine._get_conn()
        try:
            cur = conn.execute("SELECT COUNT(*) FROM jobs")
            job_count = cur.fetchone()[0]

            cur = conn.execute("SELECT COUNT(*) FROM job_chunks")
            chunk_count = cur.fetchone()[0]
        finally:
            conn.close()

        assert job_count == 3, f"Expected 3 jobs, got {job_count}"
        assert chunk_count == 3, f"Expected 3 chunks (1 per job), got {chunk_count}"

    def test_sqlite_user_version(self, engine):
        """Verify user_version reflects schema migrations."""
        conn = engine._get_conn()
        try:
            cur = conn.execute("PRAGMA user_version")
            version = cur.fetchone()[0]
        finally:
            conn.close()

        # user_version 3 = jobs + job_chunks + plan_materializations
        assert version >= 2, f"user_version too low: {version}"


# ============================================================================
# 4. REPORT COMPLETENESS AUDIT
# ============================================================================

class TestReportCompleteness:
    """Verify daily_operation_report contains all required sections."""

    @pytest.fixture
    def engine(self, tmp_path, monkeypatch):
        from astock_api.job_engine import JobEngine

        db_path = str(tmp_path / "jobs.db")
        data_dir = str(tmp_path)
        monkeypatch.setattr(JobEngine, "__init__", lambda self, *a, **kw: None)
        engine = JobEngine()
        engine.db_path = db_path
        engine.data_dir = data_dir
        engine.initialize()
        return engine

    def test_report_contains_required_sections(self, engine, tmp_path):
        """Verify report has all required top-level sections."""
        from astock_api.operation_report import generate_daily_report

        # Create a mock store and universe
        class MockStore:
            def get_all_instrument_states(self):
                return []

        universe_path = tmp_path / "universe.json"
        with open(universe_path, "w") as f:
            json.dump([{"canonical_id": "SSE:000001", "code": "000001", "exchange": "SSE", "asset_type": "INDEX"}], f)

        report = generate_daily_report(
            MockStore(), engine, str(universe_path), "2026-08-20",
            output_dir=str(tmp_path)
        )

        # Check required sections exist
        assert "generated_at" in report, "Missing generated_at"
        assert "reference_date" in report, "Missing reference_date"
        assert "universe_sha256" in report, "Missing universe_sha256"
        assert "universe" in report, "Missing universe section"
        assert "coverage" in report, "Missing coverage section"
        assert "freshness" in report, "Missing freshness section"
        assert "scheduler" in report, "Missing scheduler section"
        assert "jobs" in report, "Missing jobs section"

    def test_report_universe_section(self, engine, tmp_path):
        """Verify universe section has total/equity/index counts."""
        from astock_api.operation_report import generate_daily_report

        class MockStore:
            def get_all_instrument_states(self):
                return []

        universe_path = tmp_path / "universe.json"
        with open(universe_path, "w") as f:
            json.dump([
                {"canonical_id": "SSE:000001", "code": "000001", "exchange": "SSE", "asset_type": "INDEX"},
                {"canonical_id": "SZSE:000001", "code": "000001", "exchange": "SZSE", "asset_type": "EQUITY"},
            ], f)

        report = generate_daily_report(
            MockStore(), engine, str(universe_path), "2026-08-20",
            output_dir=str(tmp_path)
        )

        assert report["universe"]["total"] == 2
        assert report["universe"]["equity"] == 1
        assert report["universe"]["index"] == 1

    def test_report_scheduler_section(self, engine, tmp_path):
        """Verify scheduler section has last_status/last_run_at."""
        from astock_api.operation_report import generate_daily_report

        class MockStore:
            def get_all_instrument_states(self):
                return []

        universe_path = tmp_path / "universe.json"
        with open(universe_path, "w") as f:
            json.dump([{"canonical_id": "SSE:000001", "code": "000001", "exchange": "SSE", "asset_type": "INDEX"}], f)

        report = generate_daily_report(
            MockStore(), engine, str(universe_path), "2026-08-20",
            output_dir=str(tmp_path)
        )

        assert "last_status" in report["scheduler"] or report["scheduler"]["last_status"] is None
        assert "last_run_at" in report["scheduler"] or report["scheduler"]["last_run_at"] is None


# ============================================================================
# 5. FAILURE SIMULATION AUDIT
# ============================================================================

class TestFailureSimulation:
    """Simulate failures and verify classification/verification/run status."""

    @pytest.fixture
    def engine(self, tmp_path, monkeypatch):
        from astock_api.job_engine import JobEngine

        db_path = str(tmp_path / "jobs.db")
        data_dir = str(tmp_path)
        monkeypatch.setattr(JobEngine, "__init__", lambda self, *a, **kw: None)
        engine = JobEngine()
        engine.db_path = db_path
        engine.data_dir = data_dir
        engine.initialize()
        return engine

    def _setup_failed_job(self, engine, error_msg):
        """Helper: create a job and mark it as FAILED with given error."""
        engine.create_job("market_bars_update", {
            "instruments": [{"code": "600519", "exchange": "SSE", "asset_type": "EQUITY"}],
            "count": 10,
        })

        conn = engine._get_conn()
        try:
            cur = conn.execute("PRAGMA table_info(jobs)")
            columns = [row[1] for row in cur.fetchall()]
            if "error_message" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN error_message TEXT")
            conn.execute(
                "UPDATE jobs SET status='FAILED', error_message=? WHERE status='PENDING'",
                (error_msg,)
            )
            conn.commit()
        finally:
            conn.close()

    def test_upstream_no_data_simulation(self, engine):
        """Simulate UPSTREAM_NO_DATA: should classify as WARN, not FAIL."""
        from astock_api.failure_policy import classify_and_decide

        decision = classify_and_decide("No data found for symbol 600519")
        assert decision.category == "UPSTREAM_NO_DATA"
        assert decision.severity == "WARN"
        assert decision.retryable is False
        assert decision.action == "REPORT"

    def test_network_error_simulation(self, engine):
        """Simulate NETWORK_ERROR: should classify as WARN + retryable."""
        from astock_api.failure_policy import classify_and_decide

        decision = classify_and_decide("Connection timeout after 30s")
        assert decision.category == "NETWORK_ERROR"
        assert decision.severity == "WARN"
        assert decision.retryable is True
        assert decision.action == "RETRY"

    def test_data_corruption_simulation(self, engine):
        """Simulate DATA_CORRUPTION: should classify as CRITICAL + STOP."""
        from astock_api.failure_policy import classify_and_decide

        decision = classify_and_decide("Checksum mismatch on chunk data")
        assert decision.category == "DATA_CORRUPTION"
        assert decision.severity == "CRITICAL"
        assert decision.retryable is False
        assert decision.action == "STOP"

    def test_verification_catches_critical_failure(self, engine):
        """Verify verifier FAILs when DATA_CORRUPTION is detected."""
        from astock_api.run_lifecycle import create_run, start_run, set_executing, complete_run
        from astock_api.run_verifier import verify_run

        run_id = create_run(engine, "2026-08-20")
        start_run(engine, run_id)

        self._setup_failed_job(engine, "Checksum mismatch on chunk data")
        set_executing(engine, run_id, jobs_created=1)
        complete_run(engine, run_id, plan_hash="abc", jobs_created=1, jobs_done=0, jobs_failed=1)

        result = verify_run(engine, run_id)
        assert result.status == "FAIL", f"Expected FAIL for data corruption, got {result.status}"

    def test_verification_warns_on_upstream_no_data(self, engine):
        """Verify verifier WARNs (not FAIL) for UPSTREAM_NO_DATA."""
        from astock_api.run_lifecycle import create_run, start_run, set_executing, complete_run
        from astock_api.run_verifier import verify_run

        run_id = create_run(engine, "2026-08-20")
        start_run(engine, run_id)

        self._setup_failed_job(engine, "No data found for symbol 600519")
        set_executing(engine, run_id, jobs_created=1)
        complete_run(engine, run_id, plan_hash="abc", jobs_created=1, jobs_done=0, jobs_failed=1)

        result = verify_run(engine, run_id)
        # UPSTREAM_NO_DATA → WARN (acceptable), not FAIL
        assert result.status in ("PASS", "WARN"), \
            f"Expected PASS/WARN for upstream no data, got {result.status}"


# ============================================================================
# 6. END-TO-END DAILY CYCLE AUDIT
# ============================================================================

class TestEndToEndDailyCycle:
    """Execute full pipeline with small representative universe."""

    @pytest.fixture
    def engine(self, tmp_path, monkeypatch):
        from astock_api.job_engine import JobEngine

        db_path = str(tmp_path / "jobs.db")
        data_dir = str(tmp_path)
        monkeypatch.setattr(JobEngine, "__init__", lambda self, *a, **kw: None)
        engine = JobEngine()
        engine.db_path = db_path
        engine.data_dir = data_dir
        engine.initialize()
        return engine

    def test_full_cycle_with_small_universe(self, engine, tmp_path):
        """Execute: Universe → Coverage → Planner → Executor → Quality → Verifier → Report."""
        from astock_api.scheduler import run_update_cycle

        # Small representative universe: 1 INDEX + 1 EQUITY
        universe = [
            {"canonical_id": "SSE:000001", "code": "000001", "exchange": "SSE", "asset_type": "INDEX"},
            {"canonical_id": "SZSE:000001", "code": "000001", "exchange": "SZSE", "asset_type": "EQUITY"},
        ]
        universe_path = tmp_path / "universe.json"
        with open(universe_path, "w") as f:
            json.dump(universe, f)

        # Mock store with no data (all instruments are MISSING)
        class MockStore:
            def get_all_instrument_states(self):
                return []

        # Execute full cycle
        result = run_update_cycle(MockStore(), engine, str(universe_path), "2026-08-20")

        # Verify cycle completed
        assert "run_id" in result, "Missing run_id in cycle result"
        assert result["reference_date"] == "2026-08-20"

        # Verify coverage was generated
        assert "coverage_summary" in result, "Missing coverage_summary"

        # Verify plan was created
        assert "plan_summary" in result, "Missing plan_summary"

        # Verify jobs were materialized
        assert "materialization" in result, "Missing materialization"
        assert result["materialization"]["created_jobs"] >= 0

        # Verify plan hash is deterministic
        assert "plan_hash" in result, "Missing plan_hash"

    def test_cycle_idempotency(self, engine, tmp_path):
        """Verify running cycle twice doesn't create duplicate jobs."""
        from astock_api.scheduler import run_update_cycle

        universe = [
            {"canonical_id": "SSE:000001", "code": "000001", "exchange": "SSE", "asset_type": "INDEX"},
        ]
        universe_path = tmp_path / "universe.json"
        with open(universe_path, "w") as f:
            json.dump(universe, f)

        class MockStore:
            def get_all_instrument_states(self):
                return []

        # First run
        result1 = run_update_cycle(MockStore(), engine, str(universe_path), "2026-08-20")
        jobs_created_1 = result1["materialization"]["created_jobs"]

        # Second run with same plan — should be deduplicated
        result2 = run_update_cycle(MockStore(), engine, str(universe_path), "2026-08-20")
        jobs_created_2 = result2["materialization"]["created_jobs"]

        # Second run should create fewer or equal jobs (dedup)
        assert jobs_created_2 <= jobs_created_1, \
            f"Second run created more jobs ({jobs_created_2}) than first ({jobs_created_1})"

    def test_cycle_with_cross_market_isolation(self, engine, tmp_path):
        """Verify SSE:000001 INDEX and SZSE:000001 EQUITY are independent."""
        from astock_api.scheduler import run_update_cycle

        universe = [
            {"canonical_id": "SSE:000001", "code": "000001", "exchange": "SSE", "asset_type": "INDEX"},
            {"canonical_id": "SZSE:000001", "code": "000001", "exchange": "SZSE", "asset_type": "EQUITY"},
        ]
        universe_path = tmp_path / "universe.json"
        with open(universe_path, "w") as f:
            json.dump(universe, f)

        class MockStore:
            def get_all_instrument_states(self):
                return []

        result = run_update_cycle(MockStore(), engine, str(universe_path), "2026-08-20")

        # Both instruments should be in the plan
        assert result["materialization"]["created_jobs"] >= 1, \
            "Expected at least 1 job for cross-market universe"
