"""R8-1C burn-in evidence hardening: focused tests.

Covers only the R8-1C changes (NOT a re-run of R8-1/R8-1B validation):
  1. _worker_status: unstarted worker  -> alive=false, NOT_RUNNING, ORCHESTRATION_ONLY
  2. _worker_status: started worker    -> alive=true,  RUNNING (no mode)
     (+ legacy engine without the health API -> NOT_RUNNING, never placeholder True)
  3. script entrypoint portability: scripts/run_burnin_validation.py must
     locate docker_api/src itself without an external PYTHONPATH.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]  # docker_api/
SCRIPTS = REPO / "scripts"
SCRIPT = SCRIPTS / "run_burnin_validation.py"


# ---------------------------------------------------------------------------
# 1+2. _worker_status reporting accuracy
# ---------------------------------------------------------------------------

def test_worker_not_running_reports_false(tmp_path):
    """Unstarted JobEngine: snapshot must NOT claim the worker is alive."""
    from astock_api.job_engine import JobEngine
    from astock_api.burnin import _worker_status

    engine = JobEngine(db_path=str(tmp_path / "j.db"), data_dir=str(tmp_path))
    engine.initialize()  # no start() — worker thread never created

    status = _worker_status(engine)
    assert status["alive"] is False
    assert status["state"] == "NOT_RUNNING"
    assert status.get("mode") == "ORCHESTRATION_ONLY"


def test_worker_running_reports_true(tmp_path):
    """Started JobEngine: snapshot must report the real live thread."""
    from astock_api.job_engine import JobEngine
    from astock_api.burnin import _worker_status

    engine = JobEngine(db_path=str(tmp_path / "j.db"), data_dir=str(tmp_path))
    engine.initialize()
    engine.start(handler_func=lambda job: None)
    try:
        assert engine.get_worker_health()["thread_alive"] is True
        status = _worker_status(engine)
        assert status["alive"] is True
        assert status["state"] == "RUNNING"
        assert "mode" not in status  # mode is reserved for orchestration-only
    finally:
        engine.stop()


def test_worker_status_final_report_fields(tmp_path):
    """finish_burn_in must surface state/mode from the real snapshot,
    not the old placeholder alive_last_cycle=True."""
    from astock_api.job_engine import JobEngine
    from astock_api.burnin import (
        start_burn_in, run_burn_in, finish_burn_in,
    )

    class MockStore:
        def __init__(self, duckdb_path):
            import duckdb
            self.duckdb_path = duckdb_path
            conn = duckdb.connect(duckdb_path)
            conn.execute("CREATE TABLE IF NOT EXISTS market_bars_daily (id INTEGER)")
            conn.close()

        def get_all_instrument_states(self):
            return []

    engine = JobEngine(db_path=str(tmp_path / "j.db"), data_dir=str(tmp_path / "d"))
    os.makedirs(tmp_path / "d", exist_ok=True)
    engine.initialize()
    store = MockStore(str(tmp_path / "d" / "x.duckdb"))

    universe_path = tmp_path / "universe.json"
    universe_path.write_text(
        '[{"canonical_id": "SZSE:000001", "code": "000001", '
        '"exchange": "SZSE", "asset_type": "EQUITY"}]'
    )

    session = start_burn_in(
        total_cycles=1,
        reference_dates=["2026-08-21"],
        store=store,
        engine=engine,
        universe_path=str(universe_path),
        output_dir=str(tmp_path / "reports"),
    )
    run_burn_in(session)
    report = finish_burn_in(session)

    wh = report["worker_health"]
    # per-cycle snapshot was NOT_RUNNING (worker never started in harness)
    assert wh["alive_last_cycle"] is False
    assert wh["state_last_cycle"] == "NOT_RUNNING"
    assert wh["mode"] == "ORCHESTRATION_ONLY"
    assert wh["cycles_checked"] == 1


def test_legacy_engine_without_health_api_reports_not_running():
    """A minimal engine lacking get_worker_health must fall back to
    NOT_RUNNING — never the old placeholder {alive: True}."""
    from astock_api.burnin import _worker_status

    class LegacyEngine:
        pass  # no get_worker_health, no _started

    status = _worker_status(LegacyEngine())
    assert status["alive"] is False
    assert status["state"] == "NOT_RUNNING"


# ---------------------------------------------------------------------------
# 3. script entrypoint portability (no external PYTHONPATH)
# ---------------------------------------------------------------------------

def test_script_bootstrap_resolves_project_src():
    """The script's _bootstrap_path must point at docker_api/src (the
    sibling of scripts/), not the non-existent scripts/src."""
    text = SCRIPT.read_text()
    assert "def _bootstrap_path" in text, "R8-1C _bootstrap_path missing"

    start = text.index("def _bootstrap_path")
    end = text.rindex("_bootstrap_path()") + len("_bootstrap_path()")
    bootstrap = text[start:end]

    ns: dict = {"os": os, "sys": sys, "__file__": str(SCRIPT)}
    exec(compile(bootstrap, str(SCRIPT), "exec"), ns)
    src = ns["_bootstrap_path"]()

    expected = (SCRIPTS / ".." / "src").resolve()
    assert Path(src).resolve() == expected
    assert os.path.isdir(os.path.join(src, "astock_api"))
    # the old broken path must not be what the script resolves to
    assert not os.path.isdir(str(SCRIPTS / "src"))


def test_script_bootstrap_enables_import_without_pythonpath():
    """End-to-end: exec the script's own bootstrap in a clean subprocess
    (PYTHONPATH stripped, cwd=scripts/) and import astock_api."""
    text = SCRIPT.read_text()
    start = text.index("def _bootstrap_path")
    end = text.rindex("_bootstrap_path()") + len("_bootstrap_path()")
    bootstrap = text[start:end]
    code = (
        "import os, sys\n"
        f"__file__ = {str(SCRIPT)!r}\n"
        + bootstrap
        + "\nimport astock_api\nprint('IMPORT_OK', astock_api.__file__)"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, cwd=str(SCRIPTS),
        timeout=120,
    )
    assert r.returncode == 0, f"bootstrap+import failed:\n{r.stderr}"
    assert "IMPORT_OK" in r.stdout
