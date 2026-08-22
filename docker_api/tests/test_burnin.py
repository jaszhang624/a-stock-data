"""R8-1 Burn-In Controller targeted tests.

Focused coverage (does NOT repeat R5–R7 unit tests):
1. Session creation + input validation
2. Single cycle execution (snapshot fields, persistence)
3. Multi-cycle consecutive execution (distinct plan hashes)
4. Failing cycle recorded, session continues, UNSTABLE grade
5. Report determinism (same input → same sha256)
6. Session persistence across restart (load/re-hydrate)
7. Final report generation (schema + grade)
8. Idempotency (duplicate reference date rejected; dedup observed)
"""

import json
import os

import pytest


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------

@pytest.fixture
def engine(tmp_path, monkeypatch):
    """Isolated JobEngine (same pattern as R7 tests)."""
    from astock_api.job_engine import JobEngine

    db_path = str(tmp_path / "jobs.db")
    data_dir = str(tmp_path)
    monkeypatch.setattr(JobEngine, "__init__", lambda self, *a, **kw: None)
    eng = JobEngine()
    eng.db_path = db_path
    eng.data_dir = data_dir
    eng.initialize()
    return eng


@pytest.fixture
def mock_store():
    """Mock DatasetStore: no instrument states (empty coverage)."""

    class _MockStore:
        def get_all_instrument_states(self):
            return []

    return _MockStore()


@pytest.fixture
def universe_path(tmp_path):
    universe = [
        {"canonical_id": "SSE:000001", "code": "000001", "exchange": "SSE", "asset_type": "INDEX"},
        {"canonical_id": "SZSE:000001", "code": "000001", "exchange": "SZSE", "asset_type": "EQUITY"},
    ]
    p = tmp_path / "universe.json"
    with open(p, "w") as f:
        json.dump(universe, f)
    return str(p)


def _make_engine(tmp_path, name="jobs2.db"):
    """Second isolated engine without fixture monkeypatch interference."""
    from astock_api.job_engine import JobEngine

    eng = JobEngine.__new__(JobEngine)
    eng.db_path = str(tmp_path / name)
    eng.data_dir = str(tmp_path / f"outdir_{name}")
    os.makedirs(eng.data_dir, exist_ok=True)
    eng.initialize()
    return eng


# ----------------------------------------------------------------------------
# 1. Session creation + validation
# ----------------------------------------------------------------------------

def test_session_creation_valid(engine, mock_store, universe_path, tmp_path):
    from astock_api.burnin import start_burn_in

    dates = [f"2026-08-2{d}" for d in range(1, 8)]
    session = start_burn_in(
        total_cycles=7,
        reference_dates=dates,
        store=mock_store,
        engine=engine,
        universe_path=universe_path,
        output_dir=str(tmp_path / "burnin"),
    )
    assert session.total_cycles == 7
    assert session.status == "RUNNING"
    assert session.session_id.startswith("burnin-")
    # state file persisted
    assert os.path.exists(session.state_path())


def test_session_creation_duplicate_dates_rejected(engine, mock_store, universe_path, tmp_path):
    """Duplicate reference dates → identical plan hashes → dedup block."""
    from astock_api.burnin import start_burn_in

    with pytest.raises(ValueError, match="distinct"):
        start_burn_in(
            total_cycles=2,
            reference_dates=["2026-08-21", "2026-08-21"],
            store=mock_store,
            engine=engine,
            universe_path=universe_path,
            output_dir=str(tmp_path / "burnin"),
        )


def test_session_creation_count_mismatch_rejected(engine, mock_store, universe_path, tmp_path):
    from astock_api.burnin import start_burn_in

    with pytest.raises(ValueError, match="must equal"):
        start_burn_in(
            total_cycles=3,
            reference_dates=["2026-08-21", "2026-08-22"],
            store=mock_store,
            engine=engine,
            universe_path=universe_path,
            output_dir=str(tmp_path / "burnin"),
        )


# ----------------------------------------------------------------------------
# 2. Single cycle execution
# ----------------------------------------------------------------------------

def test_single_cycle_execution(engine, mock_store, universe_path, tmp_path):
    from astock_api.burnin import start_burn_in, execute_burn_in_cycle

    session = start_burn_in(
        total_cycles=1,
        reference_dates=["2026-08-21"],
        store=mock_store,
        engine=engine,
        universe_path=universe_path,
        output_dir=str(tmp_path / "burnin"),
    )

    snap = execute_burn_in_cycle(session)

    assert snap["cycle"] == 1
    assert snap["status"] == "SUCCESS"
    assert snap["run_id"] is not None
    assert snap["plan_hash"] is not None
    assert snap["report_sha256"] is not None and len(snap["report_sha256"]) == 64
    # snapshot file + operation report written
    assert os.path.exists(os.path.join(session.output_dir, "cycle_001", "cycle_001.json"))
    assert os.path.exists(os.path.join(session.output_dir, "cycle_001", "daily_operation_report.json"))
    assert len(session.cycles) == 1


# ----------------------------------------------------------------------------
# 3. Multi-cycle consecutive execution
# ----------------------------------------------------------------------------

def test_multi_cycle_consecutive(engine, mock_store, universe_path, tmp_path):
    from astock_api.burnin import start_burn_in, run_burn_in

    dates = ["2026-08-21", "2026-08-22", "2026-08-23"]
    session = start_burn_in(
        total_cycles=3,
        reference_dates=dates,
        store=mock_store,
        engine=engine,
        universe_path=universe_path,
        output_dir=str(tmp_path / "burnin"),
    )

    run_burn_in(session)

    assert len(session.cycles) == 3
    assert [c["reference_date"] for c in session.cycles] == dates
    assert all(c["status"] == "SUCCESS" for c in session.cycles)
    # distinct runs per cycle
    run_ids = [c["run_id"] for c in session.cycles]
    assert len(set(run_ids)) == 3, f"Run IDs must be distinct, got {run_ids}"
    # no unfinished runs left behind
    from astock_api.run_lifecycle import get_runs
    runs = get_runs(engine, limit=10)
    assert all(r["status"] in ("SUCCESS", "FAILED") for r in runs)


# ----------------------------------------------------------------------------
# 4. Failing cycle recorded, session continues
# ----------------------------------------------------------------------------

def test_failing_cycle_recorded_then_continues(engine, mock_store, universe_path, tmp_path, monkeypatch):
    """A failing cycle is recorded (not raised); later cycles proceed."""
    from astock_api import scheduler as sched
    from astock_api.burnin import start_burn_in, execute_burn_in_cycle, finish_burn_in

    session = start_burn_in(
        total_cycles=2,
        reference_dates=["2026-08-21", "2026-08-22"],
        store=mock_store,
        engine=engine,
        universe_path=universe_path,
        output_dir=str(tmp_path / "burnin"),
    )

    def fake_cycle(*a, **kw):
        raise RuntimeError("simulated production failure")

    monkeypatch.setattr(sched, "run_update_cycle", fake_cycle)
    snap0 = execute_burn_in_cycle(session)  # must not raise
    monkeypatch.undo()
    snap1 = execute_burn_in_cycle(session)

    assert snap0["status"] == "FAILED"
    assert "simulated production failure" in snap0["error"]
    assert snap1["status"] == "SUCCESS"

    report = finish_burn_in(session)
    assert report["grade"] == "UNSTABLE"
    assert report["cycles_failed"] == 1
    assert report["cycles_successful"] == 1
    assert len(report["failed_cycles"]) == 1
    assert session.status == "FINISHED"


# ----------------------------------------------------------------------------
# 5. Report determinism
# ----------------------------------------------------------------------------

def test_report_determinism_same_input_same_sha256(engine, mock_store, universe_path, tmp_path):
    """Same universe + same reference date → identical canonical sha256."""
    from astock_api.burnin import start_burn_in, execute_burn_in_cycle

    session = start_burn_in(
        total_cycles=1,
        reference_dates=["2026-08-21"],
        store=mock_store,
        engine=engine,
        universe_path=universe_path,
        output_dir=str(tmp_path / "burnin"),
    )
    snap1 = execute_burn_in_cycle(session)
    first_sha = snap1["report_sha256"]
    assert first_sha is not None and len(first_sha) == 64

    # Fresh engine (no jobs), same inputs, same date → same digest.
    # The plan is deduped (R6-7A) but the report content is state-derived,
    # so it must be identical after excluding the wall-clock timestamp.
    eng2 = _make_engine(tmp_path)
    session2 = start_burn_in(
        total_cycles=1,
        reference_dates=["2026-08-21"],
        store=mock_store,
        engine=eng2,
        universe_path=universe_path,
        output_dir=str(tmp_path / "burnin2"),
    )
    snap2 = execute_burn_in_cycle(session2)
    assert snap2["report_sha256"] == first_sha, \
        f"Report not deterministic: {first_sha} != {snap2['report_sha256']}"
    assert snap2["plan_hash"] == snap1["plan_hash"]


# ----------------------------------------------------------------------------
# 6. Session persistence across restart
# ----------------------------------------------------------------------------

def test_session_persistence_after_restart(engine, mock_store, universe_path, tmp_path):
    """State file survives; BurnInSession.load re-hydrates it."""
    from astock_api.burnin import start_burn_in, execute_burn_in_cycle, BurnInSession

    session = start_burn_in(
        total_cycles=3,
        reference_dates=["2026-08-21", "2026-08-22", "2026-08-23"],
        store=mock_store,
        engine=engine,
        universe_path=universe_path,
        output_dir=str(tmp_path / "burnin"),
    )
    execute_burn_in_cycle(session)
    execute_burn_in_cycle(session)

    # "restart": fresh engine + load from state file
    eng2 = _make_engine(tmp_path, name="jobs3.db")
    state_path = session.state_path()
    loaded = BurnInSession.load(state_path, store=mock_store, engine=eng2)
    assert loaded.session_id == session.session_id
    assert loaded.total_cycles == 3
    assert len(loaded.cycles) == 2
    assert loaded.cycles[0]["run_id"] == session.cycles[0]["run_id"]

    # remaining cycle executable on the re-hydrated session
    execute_burn_in_cycle(loaded)
    assert len(loaded.cycles) == 3
    assert os.path.exists(os.path.join(loaded.output_dir, "cycle_003", "cycle_003.json"))


# ----------------------------------------------------------------------------
# 7. Final report generation
# ----------------------------------------------------------------------------

def test_final_report_schema(engine, mock_store, universe_path, tmp_path):
    from astock_api.burnin import start_burn_in, run_burn_in, finish_burn_in

    session = start_burn_in(
        total_cycles=3,
        reference_dates=["2026-08-21", "2026-08-22", "2026-08-23"],
        store=mock_store,
        engine=engine,
        universe_path=universe_path,
        output_dir=str(tmp_path / "burnin"),
    )
    run_burn_in(session)
    report = finish_burn_in(session)

    for field in (
        "session_id", "started_at", "finished_at", "total_cycles",
        "cycles_executed", "cycles_successful", "cycles_failed",
        "worker_health", "database_growth", "quality_summary",
        "state_consistency", "idempotency", "grade",
    ):
        assert field in report, f"final report missing {field}"

    assert report["grade"] == "STABLE"
    assert report["cycles_successful"] == 3
    assert report["state_consistency"]["consistent"] is True
    assert report["idempotency"]["distinct_plan_hashes"] == 3
    assert report["idempotency"]["duplicate_plan_hashes"] is False
    assert report["worker_health"]["cycles_checked"] == 3
    # artifact written
    assert os.path.exists(os.path.join(session.output_dir, "burnin_final_report.json"))


def test_health_burnin_endpoint(engine, mock_store, universe_path, tmp_path, monkeypatch):
    """GET /health/burnin reflects the on-disk session state (read-only)."""
    from astock_api.burnin import start_burn_in, execute_burn_in_cycle

    out = str(tmp_path / "burnin" / "s1")
    session = start_burn_in(
        total_cycles=2,
        reference_dates=["2026-08-21", "2026-08-22"],
        store=mock_store,
        engine=engine,
        universe_path=universe_path,
        output_dir=out,
    )
    execute_burn_in_cycle(session)

    # health.py globs a fixed CWD-relative path; point it at the tmp tree
    import astock_api.health as health
    monkeypatch.setattr(health.os.path, "join", staticmethod(
        lambda *parts: "/".join(parts[1:]) if parts[0] == "data" else os.path.join(*parts)
    )) if False else None
    # Simpler: call the handler function directly after chdir-independent fix
    from astock_api.health import health_burnin
    import json as _json

    # The handler scans CWD "data/reports/burnin/*"; verify the contract
    # directly against the persisted session file instead:
    state = _json.load(open(os.path.join(out, "session.json")))
    assert state["status"] == "RUNNING"
    assert len(state["cycles"]) == 1

    # Full endpoint integration: run the handler with the glob target
    # redirected via a chdir into a fixture CWD
    fake_cwd = tmp_path / "cwd" / "data" / "reports" / "burnin"
    fake_cwd.mkdir(parents=True)
    (fake_cwd / "s1").mkdir()
    _json.dump(
        {"status": "RUNNING", "session_id": session.session_id,
         "total_cycles": 2, "cycles": state["cycles"]},
        open(fake_cwd / "s1" / "session.json", "w"),
    )
    real_cwd = os.getcwd()
    os.chdir(tmp_path / "cwd")
    try:
        import asyncio
        result = asyncio.run(health_burnin())
    finally:
        os.chdir(real_cwd)

    assert result["active"] is True
    assert result["session_id"] == session.session_id
    assert result["cycle_completed"] == 1
    assert result["total_cycles"] == 2
    assert result["last_status"] == "SUCCESS"
    assert result["last_snapshot"] == "cycle_001.json"


# ----------------------------------------------------------------------------
# 8. Idempotency: same plan hash across engines, dedup blocks re-creation
# ----------------------------------------------------------------------------

def test_idempotent_plan_materialization(engine, mock_store, universe_path, tmp_path):
    """Two engines, same (universe, reference_date): plan_hash identical;
    same-engine re-run is deduped (R6-7A)."""
    from astock_api.burnin import start_burn_in, execute_burn_in_cycle
    from astock_api.scheduler import run_update_cycle

    eng2 = _make_engine(tmp_path, name="jobs4.db")
    s1 = start_burn_in(
        total_cycles=1,
        reference_dates=["2026-08-21"],
        store=mock_store,
        engine=engine,
        universe_path=universe_path,
        output_dir=str(tmp_path / "b1"),
    )
    s2 = start_burn_in(
        total_cycles=1,
        reference_dates=["2026-08-21"],
        store=mock_store,
        engine=eng2,
        universe_path=universe_path,
        output_dir=str(tmp_path / "b2"),
    )
    snap1 = execute_burn_in_cycle(s1)
    snap2 = execute_burn_in_cycle(s2)
    assert snap1["plan_hash"] == snap2["plan_hash"]

    # Same engine, second identical cycle → dedup should mark it
    # already_materialized (R6-7A). Burn-in session rejects duplicate dates,
    # so exercise dedup directly through the scheduler path:
    from astock_api.scheduler import run_update_cycle

    summary1 = run_update_cycle(
        mock_store, engine, universe_path,
        reference_date="2026-08-24", output_dir=str(tmp_path / "d1"),
    )
    summary2 = run_update_cycle(
        mock_store, engine, universe_path,
        reference_date="2026-08-24", output_dir=str(tmp_path / "d2"),
    )
    assert summary1["plan_hash"] == summary2["plan_hash"]
    assert summary2["materialization"]["already_materialized"] is True
    assert summary2["materialization"]["created_jobs"] == 0
