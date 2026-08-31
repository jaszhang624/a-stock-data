"""R8-1 operational validation: 3-cycle burn-in on isolated data.

Isolated artifacts:
  - SQLite jobs DB   : validation_tmp/burnin_jobs.db
  - DuckDB data plane: validation_tmp/burnin_data.duckdb
  - Output reports   : validation_tmp/burnin_reports

Verifies 7 invariants after the run:
  1. no orphan RUNNING jobs
  2. no unfinished update_runs
  3. no duplicate plan materialization
  4. no duplicate DuckDB rows
  5. no identity contamination
  6. no quality regression
  7. no database corruption (SQLite PRAGMA integrity_check)
"""
import json
import os
import shutil
import sqlite3
import sys

def _bootstrap_path():
    """Locate the project ``src/`` relative to this script (R8-1C).

    The script lives in ``docker_api/scripts/`` while the package lives in
    ``docker_api/src/`` — so the src dir is ``..`` up from the script, not a
    ``src/`` child of it. This removes the old dependency on an externally
    exported PYTHONPATH.
    """
    src = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
    if os.path.isdir(src) and src not in sys.path:
        sys.path.insert(0, src)
    return src

_bootstrap_path()

BASE = "validation_tmp"
shutil.rmtree(BASE, ignore_errors=True)
os.makedirs(BASE, exist_ok=True)

from astock_api.job_engine import JobEngine  # noqa: E402
from astock_api.burnin import (  # noqa: E402
    start_burn_in, run_burn_in, finish_burn_in,
)

# ---- isolated artifacts ----------------------------------------------------
db_path = os.path.join(BASE, "burnin_jobs.db")
data_dir = os.path.join(BASE, "data_out")
os.makedirs(data_dir, exist_ok=True)

engine = JobEngine(db_path=db_path, data_dir=data_dir)
engine.initialize()


class MockStore:
    """Empty data plane (no instrument states) + real DuckDB file for sizing."""
    def __init__(self, duckdb_path):
        import duckdb
        self.duckdb_path = duckdb_path
        # create an empty (but valid) DuckDB file so storage sizing works
        conn = duckdb.connect(duckdb_path)
        conn.execute("CREATE TABLE IF NOT EXISTS market_bars_daily (id INTEGER)")
        conn.close()

    def get_all_instrument_states(self):
        return []


duckdb_path = os.path.join(BASE, "burnin_data.duckdb")
store = MockStore(duckdb_path)

# small representative universe (SSE index + SZSE equity — cross-market)
universe = [
    {"canonical_id": "SSE:000001", "code": "000001", "exchange": "SSE", "asset_type": "INDEX"},
    {"canonical_id": "SZSE:000001", "code": "000001", "exchange": "SZSE", "asset_type": "EQUITY"},
]
universe_path = os.path.join(BASE, "universe.json")
with open(universe_path, "w") as f:
    json.dump(universe, f)

output_dir = os.path.join(BASE, "burnin_reports")
dates = ["2026-08-21", "2026-08-22", "2026-08-23"]

# ---- run burn-in -----------------------------------------------------------
print("=== START BURN-IN (3 cycles) ===")
session = start_burn_in(
    total_cycles=3,
    reference_dates=dates,
    store=store,
    engine=engine,
    universe_path=universe_path,
    output_dir=output_dir,
)
print(f"session: {session.session_id}")

run_burn_in(session)
report = finish_burn_in(session)

for c in session.cycles:
    sha = c.get("report_sha256") or ""
    print(f"  cycle {c['cycle']} ({c['reference_date']}): {c['status']} "
          f"run={c['run_id']} plan={str(c['plan_hash'])[:12]} jobs_created={c['jobs']['created']} "
          f"duration={c['duration_s']}s sha256={sha[:12]}")

# ---- invariant checks ------------------------------------------------------
print("\n=== INVARIANT CHECKS ===")
conn = sqlite3.connect(db_path)

# 1. orphan RUNNING jobs
orphans = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='RUNNING'").fetchone()[0]
print(f"1. orphan RUNNING jobs: {orphans} {'PASS' if orphans == 0 else 'FAIL'}")

# 2. unfinished update_runs
unfinished = conn.execute(
    "SELECT COUNT(*) FROM update_runs WHERE status NOT IN ('SUCCESS','FAILED','INTERRUPTED')"
).fetchone()[0]
statuses = [r[0] for r in conn.execute("SELECT status FROM update_runs").fetchall()]
print(f"2. unfinished update_runs: {unfinished} (statuses={statuses}) {'PASS' if unfinished == 0 else 'FAIL'}")

# 3. duplicate plan materialization
mat = conn.execute(
    "SELECT plan_hash, COUNT(*) FROM plan_materializations GROUP BY plan_hash HAVING COUNT(*) > 1"
).fetchall()
total_mat = conn.execute("SELECT COUNT(*) FROM plan_materializations").fetchone()[0]
print(f"3. duplicate plan materialization: {len(mat)} dups (total rows={total_mat}) {'PASS' if not mat else 'FAIL'}")

# 4. duplicate DuckDB rows (empty plane — must be 0)
import duckdb
dconn = duckdb.connect(duckdb_path, read_only=True)
dup_rows = dconn.execute("SELECT COUNT(*) FROM market_bars_daily").fetchone()[0]
print(f"4. DuckDB rows: {dup_rows} {'PASS' if dup_rows == 0 else 'FAIL'}")
dconn.close()

# 5. identity contamination — no cross-market identity in jobs params
rows = conn.execute("SELECT params_json FROM jobs").fetchall()
contamination = 0
for (pj,) in rows:
    if not pj:
        continue
    try:
        params = json.loads(pj)
    except (json.JSONDecodeError, TypeError):
        continue
    s = json.dumps(params)
    # canonical IDs must appear in proper EXCHANGE:CODE form only
    for frag in ["SSE:SZSE", "SZSE:SSE"]:
        if frag in s:
            contamination += 1
print(f"5. identity contamination: {contamination} {'PASS' if contamination == 0 else 'FAIL'}")

# 6. quality regression — no FAILED quality status in update_runs
qfail = conn.execute(
    "SELECT COUNT(*) FROM update_runs WHERE quality_status = 'FAIL'"
).fetchone()[0]
print(f"6. quality FAIL runs: {qfail} {'PASS' if qfail == 0 else 'FAIL'}")

# 7. SQLite integrity
integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
conn.close()
print(f"7. SQLite integrity_check: {integrity} {'PASS' if integrity == 'ok' else 'FAIL'}")

# ---- final report ----------------------------------------------------------
print("\n=== FINAL BURN-IN REPORT ===")
print(json.dumps(report, indent=2, default=str))
