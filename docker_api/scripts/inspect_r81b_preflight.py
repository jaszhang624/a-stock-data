"""R8-1B pre-flight inspection (read-only)."""
import json
import sqlite3
import sys
from collections import Counter

import duckdb

sys.path.insert(0, "src")

# 1. real duckdb: tables + row counts
d = duckdb.connect("data/astock_data.duckdb", read_only=True)
tables = [r[0] for r in d.execute(
    "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
).fetchall()]
print("DuckDB tables:", tables)
for t in tables:
    try:
        n = d.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t}: {n} rows")
    except Exception as e:
        print(f"  {t}: ERR {e}")
d.close()

# 2. universe: target instruments + size
uni = json.load(open("data/universe/instrument_universe_v2.json"))
print("\nuniverse size:", len(uni))
hits = [u for u in uni if u.get("canonical_id") in ("SSE:000001", "SZSE:000001")]
print("targets:", json.dumps(hits, indent=1))
print("exchanges:", dict(Counter(u.get("exchange") for u in uni)))
print("asset_types:", dict(Counter(u.get("asset_type") for u in uni)))

# 3. validation DB: what happened to the 12 jobs
conn = sqlite3.connect("validation_tmp/burnin_jobs.db")
print("\nvalidation jobs status:", conn.execute(
    "SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
print("sample job params:")
for r in conn.execute("SELECT id, status, params_json FROM jobs LIMIT 3").fetchall():
    print(" ", r[0], r[1], (r[2] or "")[:160])
conn.close()

# 4. DatasetStore API surface for the real store
from astock_api.dataset_store import DatasetStore
print("\nDatasetStore methods:", [m for m in dir(DatasetStore) if not m.startswith("_")])
