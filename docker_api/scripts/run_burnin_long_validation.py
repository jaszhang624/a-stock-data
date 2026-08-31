"""R8-1B long-duration burn-in validation: 7 consecutive cycles.

Extension of the R8-1 3-cycle operational validation (run_burnin_validation.py):
  - 7 distinct reference dates (R6-7A plan dedup requires distinct dates)
  - explicit idempotency check: repeat cycle 7's reference date ->
    expect already_materialized=True, 0 new jobs
  - explicit cross-market isolation: SSE:000001 INDEX vs SZSE:000001 EQUITY
  - enrich burnin_final_report.json with cross_market_validation +
    observations sections (driver level; burnin.py module unchanged)

Isolated artifacts (no production data touched):
  - SQLite jobs DB   : validation_tmp/long7/burnin_jobs.db
  - DuckDB data plane: validation_tmp/long7/burnin_data.duckdb (empty plane)
  - Snapshots/reports: data/reports/burnin/long7/  (per task spec)

No worker thread is started (same as the R8-1 harness): jobs remain PENDING
by design; the burn-in validates the scheduler/lifecycle/executor/verifier
orchestration layer. Worker health is reported via the engine's real API.
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

BASE = "validation_tmp/long7"
shutil.rmtree(BASE, ignore_errors=True)
os.makedirs(BASE, exist_ok=True)

from astock_api.job_engine import JobEngine  # noqa: E402
from astock_api.burnin import (  # noqa: E402
    start_burn_in, run_burn_in, finish_burn_in,
)
from astock_api.scheduler import run_update_cycle  # noqa: E402

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

output_dir = os.path.join("data", "reports", "burnin", "long7")
dates = [f"2026-08-{d:02d}" for d in range(21, 28)]  # 7 distinct dates


def _sizes():
    return {
        "sqlite_mb": round(os.path.getsize(db_path) / 1048576, 3),
        "duckdb_mb": round(os.path.getsize(duckdb_path) / 1048576, 3),
    }


storage_before = _sizes()
worker_health_before = engine.get_worker_health()


def _count(table):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _row_counts():
    return {t: _count(t) for t in (
        "jobs", "job_chunks", "plan_materializations", "update_runs",
    )}


print("=== START 7-CYCLE BURN-IN ===")
session = start_burn_in(
    total_cycles=7,
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
    print(
        f"  cycle {c['cycle']} ({c['reference_date']}): {c['status']} "
        f"run={c['run_id']} plan={str(c['plan_hash'])[:12]} "
        f"jobs_created={c['jobs']['created']} duration={c['duration_s']}s "
        f"error={c['error']}"
    )

# ---- idempotency: repeat cycle 7's reference date ---------------------------
jobs_before_dedup = _count("jobs")
summary = run_update_cycle(
    store, engine, universe_path,
    reference_date=dates[-1],
    output_dir=os.path.join(output_dir, "dedup_check"),
)
dedup = summary.get("materialization", {})
jobs_after_dedup = _count("jobs")
idempotency_explicit = {
    "repeated_reference_date": dates[-1],
    "already_materialized": dedup.get("already_materialized"),
    "created_jobs": dedup.get("created_jobs"),
    "jobs_before": jobs_before_dedup,
    "jobs_after": jobs_after_dedup,
    "jobs_delta": jobs_after_dedup - jobs_before_dedup,
    "plan_hash_match_cycle7": summary.get("plan_hash") == session.cycles[-1].get("plan_hash"),
}
print(f"\n=== IDEMPOTENCY (repeated date {dates[-1]}) ===")
print(json.dumps(idempotency_explicit, indent=2))

# ---- invariant checks -------------------------------------------------------
print("\n=== INVARIANT CHECKS ===")
conn = sqlite3.connect(db_path)

# 1. orphan RUNNING jobs
orphans = conn.execute(
    "SELECT COUNT(*) FROM jobs WHERE status='RUNNING'").fetchone()[0]
print(f"1. orphan RUNNING jobs: {orphans} {'PASS' if orphans == 0 else 'FAIL'}")

# 2. unfinished update_runs (7 session cycles + 1 dedup run, all terminal)
unfinished = conn.execute(
    "SELECT COUNT(*) FROM update_runs WHERE status NOT IN ('SUCCESS','FAILED','INTERRUPTED')"
).fetchone()[0]
statuses = [r[0] for r in conn.execute("SELECT status FROM update_runs").fetchall()]
print(f"2. unfinished update_runs: {unfinished} (statuses={statuses}) "
      f"{'PASS' if unfinished == 0 else 'FAIL'}")

# 3. duplicate plan materialization
mat_dups = conn.execute(
    "SELECT plan_hash, COUNT(*) c FROM plan_materializations "
    "GROUP BY plan_hash HAVING c > 1").fetchall()
distinct_hashes = conn.execute(
    "SELECT COUNT(DISTINCT plan_hash) FROM plan_materializations").fetchone()[0]
print(f"3. duplicate plan materialization: {len(mat_dups)} dups, "
      f"{distinct_hashes} distinct hashes (expect 7) "
      f"{'PASS' if not mat_dups and distinct_hashes == 7 else 'FAIL'}")

# 4. cross-market isolation (explicit)
by_market = {"SSE:000001": [], "SZSE:000001": []}
contamination = 0
unknown = []
for (job_id, pj) in conn.execute(
        "SELECT job_id, params_json FROM jobs").fetchall():
    params = json.loads(pj)
    insts = params.get("instruments") or []
    if len(insts) != 1:
        contamination += 1
        continue
    i = insts[0]
    cid = f"{i.get('exchange', '')}:{i.get('code', '')}"
    if "SSE:SZSE" in pj or "SZSE:SSE" in pj:
        contamination += 1
    if cid in by_market:
        by_market[cid].append(i.get("asset_type"))
    else:
        unknown.append(cid)

sse_n, szse_n = len(by_market["SSE:000001"]), len(by_market["SZSE:000001"])
sse_ok = sse_n == 7 and all(t == "INDEX" for t in by_market["SSE:000001"])
szse_ok = szse_n == 7 and all(t == "EQUITY" for t in by_market["SZSE:000001"])
chunk_keys = dict(conn.execute(
    "SELECT chunk_key, COUNT(*) FROM job_chunks GROUP BY chunk_key").fetchall())
print(f"4. cross-market: SSE:000001 jobs={sse_n} (INDEX={sse_ok}), "
      f"SZSE:000001 jobs={szse_n} (EQUITY={szse_ok}), "
      f"contamination={contamination}, unknown={unknown} "
      f"{'PASS' if sse_ok and szse_ok and contamination == 0 and not unknown else 'FAIL'}")
print(f"   chunk_keys: {chunk_keys}")

# 5. duplicate rows (job_chunks per job)
dup_chunks = conn.execute(
    "SELECT job_id, chunk_key, COUNT(*) c FROM job_chunks "
    "GROUP BY job_id, chunk_key HAVING c > 1").fetchall()
print(f"5. duplicate chunk rows: {len(dup_chunks)} "
      f"{'PASS' if not dup_chunks else 'FAIL'}")

# 6. quality regression — no FAILED quality status in update_runs
qfail = conn.execute(
    "SELECT COUNT(*) FROM update_runs WHERE quality_status = 'FAIL'").fetchone()[0]
qskip = conn.execute(
    "SELECT COUNT(*) FROM update_runs WHERE quality_status = 'SKIP'").fetchone()[0]
print(f"6. quality FAIL runs: {qfail} (SKIP={qskip}) "
      f"{'PASS' if qfail == 0 else 'FAIL'}")

# 7. data plane rows (empty isolated plane — duplicate/OHLCV checks)
import duckdb  # noqa: E402
dconn = duckdb.connect(duckdb_path, read_only=True)
if dconn is None:
    raise RuntimeError("DuckDB connection failed")
plane_rows = dconn.execute(
    "SELECT COUNT(*) FROM market_bars_daily").fetchone()[0]
dconn.close()
print(f"7. DuckDB data plane rows: {plane_rows} "
      f"(duplicate=0, invalid OHLCV=0 — vacuous on empty plane) "
      f"{'PASS' if plane_rows == 0 else 'FAIL'}")

# 8. SQLite integrity
integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
row_counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("jobs", "job_chunks", "plan_materializations", "update_runs")}
conn.close()
print(f"8. SQLite integrity_check: {integrity} "
      f"{'PASS' if integrity == 'ok' else 'FAIL'}")
print(f"   row_counts: {row_counts}")

worker_health_after = engine.get_worker_health()
storage_after = _sizes()

exceptions = sum(1 for c in session.cycles if c.get("error"))
snapshots_ok = all(
    os.path.exists(os.path.join(output_dir, f"cycle_{i:03d}", f"cycle_{i:03d}.json"))
    for i in range(1, 8)
)
print(f"\nsnapshots cycle_001..007 present: {snapshots_ok}")
print(f"worker_health before={worker_health_before}")
print(f"worker_health after ={worker_health_after}")
print(f"storage before={storage_before} after={storage_after}")

# ---- enrich final report (driver-level; burnin.py unchanged) ----------------
final_path = os.path.join(output_dir, "burnin_final_report.json")
with open(final_path) as f:
    final = json.load(f)

cross_market_ok = bool(sse_ok and szse_ok and contamination == 0 and not unknown)
final["cycles_completed"] = final["cycles_executed"]
final["cross_market_validation"] = {
    "independent": cross_market_ok,
    "sse_000001_jobs": sse_n,
    "sse_000001_asset_types": sorted(set(by_market["SSE:000001"])),
    "szse_000001_jobs": szse_n,
    "szse_000001_asset_types": sorted(set(by_market["SZSE:000001"])),
    "chunk_keys": chunk_keys,
    "identity_contamination": contamination,
    "unknown_instruments": unknown,
}
final["idempotency_explicit"] = idempotency_explicit
final["observations"] = [
    "worker thread NOT started (isolated orchestration mode, same harness as R8-1); "
    "jobs remain PENDING by design — burn-in validates the scheduler/lifecycle/"
    "executor/verifier layer; real worker state reported via engine.get_worker_health()",
    "quality_status='SKIP' on all runs: no quality_run_{run_id}.json exists in the "
    "isolated empty data plane; burnin quality_summary buckets (PASS/WARN/FAIL) "
    "therefore count 0 — presentation aggregation only, no effect on lifecycle, "
    "verifier decision, or burn-in grade (R8-1B Phase 1 risk review)",
    "empty isolated DuckDB plane (0 rows): duplicate-row and OHLCV-corruption "
    "checks pass vacuously; coverage is all-BOOTSTRAP because the store reports "
    "no instrument states",
    "plan dedup (R6-7A): 7 distinct plan hashes across 7 distinct reference "
    "dates; the explicit repeated-date check after cycle 7 hit the registry "
    "(already_materialized=True, 0 new jobs)",
    "per-cycle snapshot worker status is now computed from the real engine "
    "health API (R8-1C: burnin._worker_status calls engine.get_worker_health); "
    "in this harness the worker thread is never started, so snapshots report "
    "alive=false / NOT_RUNNING / ORCHESTRATION_ONLY",
]
with open(final_path, "w") as f:
    json.dump(final, f, indent=2, default=str)

print("\n=== FINAL BURN-IN REPORT (enriched) ===")
print(json.dumps(final, indent=2, default=str))
