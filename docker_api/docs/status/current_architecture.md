# a-stock-data Current Architecture Snapshot

**Status:** Frozen reference document. Describes actual implemented behavior only.
**Last Updated:** 2026-08-23
**Regression Baseline:** 617 passed, 2 skipped

---

## System Overview

a-stock-data is a market data collection and storage system for Chinese A-share equities and indices. It provides:

- Deterministic instrument universe generation
- Coverage and freshness state tracking
- Update planning with policy-driven action selection
- Job materialization with deduplication
- SQLite-backed job engine with crash recovery
- DuckDB-based market data storage

The system follows a strict separation of concerns: planning decides *what* should happen; execution handles *how*.

---

## Design Principles

1. **Identity-first** — All data operations use canonical `EXCHANGE:CODE` format. Bare symbols are never used after parsing.
2. **Idempotent operations** — All mutations (import, upsert, job creation) are idempotent.
3. **No silent data loss** — Backup before mutation; verify after execution.
4. **Planning ≠ Execution** — Planner generates decisions; Executor materializes jobs; Worker executes data collection.
5. **SQLite for metadata, DuckDB for data** — Job state lives in SQLite; market bars live in DuckDB.
6. **Reproducible builds** — Same input produces identical SHA256 output.

---

## Identity Model (R5-C4B PASS)

### Canonical Format

```
{EXCHANGE}:{CODE}
```

Examples:
- `SSE:600519` — Kweichow Moutai equity
- `SZSE:000001` — Ping An Bank equity
- `SSE:000001` — Shanghai Composite Index

### Classification Rules (EQUITY)

| Code Pattern | Exchange |
|-------------|----------|
| `6xxxxx`    | SSE      |
| `68xxxxx`   | SSE      |
| `0xxxxx`    | SZSE     |
| `3xxxxx`    | SZSE     |
| `4xxxxx`    | BSE      |
| `8xxxxx`    | BSE      |

### Critical Isolation Rule

```
SSE:000001 INDEX  ≠  SZSE:000001 EQUITY
```

Same numeric code, different exchange and asset type. They must never share state or query each other's data.

### Identity Preservation

Once parsed into canonical form, identity must never be reconstructed from bare symbol. All downstream components (DatasetStore, JobEngine, adapters) receive structured identity objects with `code`, `exchange`, and `asset_type`.

---

## Universe Model (R6-1 PASS)

### Component: `InstrumentUniverseBuilder`

Generates a deterministic, reproducible instrument list.

### Artifact: `data/universe/instrument_universe_v2.json`

- **Total instruments:** 5224
- **Equity count:** 5217 (SSE=2317, SZSE=2900)
- **Index seed count:** 7 (validated seeds only, not full universe)
- **SHA256:** `f1011f3888f4e930d9e88de2d5be8083b7cb63ea528eb234add4eb290ebca403`

### Classification Policy (v1)

- BSE explicitly skipped (unreliable TDX source)
- INDEX uses `validated_seed` strategy — only verified representative indices
- Equity drift from R5 baseline: +2 instruments

### Reproducibility

Running `universe build --source snapshot` produces identical SHA256 output. Verified via live build comparison.

---

## Data Lifecycle

```
Universe v2
  ↓
Coverage Snapshot (coverage.py)
  ↓
Freshness Assessment (freshness.py)
  ↓
UpdatePlanner (update_planner.py)
  ↓
UpdateExecutor (update_executor.py)
  ↓
Plan Dedup Registry (plan_materializations table)
  ↓
JobEngine (job_engine.py)
  ↓
Worker → SourceGovernor → Adapters
  ↓
DatasetStore (dataset_store.py)
```

---

## Coverage / Freshness Layer (R6-3 PASS)

### Component: `coverage.py`

Generates coverage snapshots by comparing Universe against DatasetStore. Uses a **single aggregate SQL query** with `GROUP BY security_id` — NOT N+1 per-instrument queries.

### Component: `freshness.py`

Assesses data freshness relative to a reference date. Three states:

| State   | Meaning                              | Action     |
|---------|--------------------------------------|------------|
| MISSING | No data exists in DatasetStore       | BOOTSTRAP  |
| CURRENT | Latest trade date matches reference  | NONE       |
| STALE   | Gap between latest and reference     | UPDATE     |

### Coverage Snapshot Output

```json
{
  "total_universe": 5224,
  "with_data": 5210,
  "missing": 14,
  "current": 1,
  "stale": 5209
}
```

### Health Endpoint

`/health/data?reference_date=YYYY-MM-DD` exposes coverage state as a lightweight read-only endpoint.

---

## Update Planning (R6-5 PASS)

### Component: `UpdatePlanner`

Converts coverage state into a deterministic update plan. Pure decision module — no execution, no JobEngine interaction.

### Policy Model: `FreshnessPolicy`

```python
FreshnessPolicy(
    reference_date="2026-08-20",
    missing_action="BOOTSTRAP",
    stale_action="UPDATE",
    current_action="NONE"
)
```

### Plan Model: `UpdatePlan` (frozen dataclass)

- `generated_at` — timestamp
- `reference_date` — policy reference date
- `universe_sha256` — universe hash for provenance
- `summary` — counts by state
- `actions` — list of `{canonical_id, action, latest_date, reason}`

### Allowed Actions

- `BOOTSTRAP` — fetch full historical data
- `UPDATE` — incremental update from latest_date
- `NONE` — no action needed

### Determinism

Same input (coverage + policy) produces identical plan. Plan hash excludes `generated_at`.

---

## Job Materialization (R6-6 PASS)

### Component: `UpdateExecutor`

Converts approved UpdatePlan actions into JobEngine jobs. Bridge between planning and execution.

### Action → Job Mapping

| Plan Action | Job Type           | Parameters                              |
|-------------|-------------------|-----------------------------------------|
| BOOTSTRAP   | market_bars_update | `bootstrap_count: 100`                  |
| UPDATE      | market_bars_update | `count: 10`, `target_date: latest_date` |
| NONE        | (skipped)         | —                                       |

### Identity Preservation

Executor splits `canonical_id` into `exchange:code`, preserves `asset_type`. Never reconstructs identity from bare symbol.

### Job Creation

Uses `JobEngine.create_job()` exclusively. Does not insert SQLite directly or create chunks manually.

---

## Deduplication Layer (R6-7A PASS)

### Purpose

Prevent duplicate materialization of the same logical update plan.

### Component: `plan_materializations` (SQLite table)

```sql
CREATE TABLE plan_materializations (
    plan_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    job_ids TEXT NOT NULL DEFAULT '[]',  -- JSON array
    status TEXT NOT NULL DEFAULT 'materialized'
)
```

### Behavior

**First execution:**
- Registry lookup: plan_hash not found
- Create jobs via JobEngine
- Store registry entry with job IDs

**Second identical execution:**
- Registry lookup: plan_hash found
- Return existing job IDs
- Create zero new jobs

**Different plan:**
- Creates jobs normally with new plan_hash

### Plan Hash Semantics

Deterministic SHA256 of plan content, excluding `generated_at`. Same logical intent produces same hash.

### Failure Safety

Registry entry written only after successful job creation. If JobEngine fails, no registry entry is created — next run retries normally.

---

## Persistence Model

### SQLite (JobEngine)

- **Role:** Job metadata, chunk states, plan materializations
- **user_version:** 3 (R6-7A added `plan_materializations`)
- **Tables:**
  - `jobs` — job lifecycle state
  - `job_chunks` — chunk execution state
  - `job_chunk_source_state` — provider-level checkpoint (R5-C2)
  - `plan_materializations` — deduplication registry (R6-7A)

### DuckDB (DatasetStore)

- **Role:** Market data storage
- **Primary key:** `(security_id, trade_date)`
- **Identity:** `security_id` uses canonical `EXCHANGE:CODE` format
- **Upsert semantics:** INSERT OR REPLACE on PK ensures idempotent writes

---

## Job Execution Layer (R5-C3 PASS)

### Component: `JobEngine`

SQLite-backed background job system with atomic writes. Single worker thread, crash recovery, retry with backoff.

### Worker Lifecycle

1. Job created via `create_job()` → PENDING
2. Worker picks up job → RUNNING
3. Chunks executed sequentially by SourceGovernor
4. Results persisted to DatasetStore
5. Job marked DONE

### Crash Recovery

On startup:
- Orphan RUNNING jobs → PENDING (restart recovery)
- Stale chunks → PENDING (recreate recovery)

### Retry Durability

Failed chunks retain retry count and next_retry_at. Backoff increases with each attempt.

---

## Source Layer

### Component: `SourceGovernor`

Routes data requests to appropriate adapters. Handles provider fallback and state tracking.

### Adapters (`upstream/`)

- `mootdx` — primary source for market bars
- `eastmoney`, `tencent`, `sina` — fallback sources
- `cninfo`, `cls`, `ths` — supplementary data

### Provider State Tracking

`job_chunk_source_state` table tracks per-provider outcomes for each chunk, enabling intelligent fallback decisions.

---

## Reconciliation Layer (R6-4 PASS)

### Component: `reconcile.py`

Imports existing R5 historical results into DatasetStore.

### Properties

- Backup before import (`astock_data.duckdb.r6-4-backup`)
- Identity preserved (EQUITY context: `6xxxxx` → SSE, `0xxxxx/3xxxxx` → SZSE)
- Idempotent import (second run produces zero additional rows)
- Batch `executemany` upsert for performance

### Verified Results

- 5209 instruments imported
- 1,035,279 rows upserted
- Zero invalid/missing/identity errors

---

## Current Data State (Post R6-4)

| Metric              | Value  |
|---------------------|--------|
| DatasetStore instruments | 5210   |
| DatasetStore rows       | 1,035,379 |
| Coverage with_data      | 5210   |
| Coverage missing        | 14     |
| Coverage current        | 1      |
| Coverage stale          | 5209   |

---

## Current Limitations

1. **Scheduler timing is manual** — `run_update_cycle` orchestrates the pipeline but is invoked on-demand; no cron/daemon loop.
2. **No automatic freshness refresh** — Coverage snapshots are generated on-demand.
3. **BSE unsupported** — TDX source unreliable for Beijing Stock Exchange instruments.
4. **INDEX limited to seeds** — Only 7 verified indices; full INDEX universe not generated.
5. **Single worker thread** — JobEngine processes jobs sequentially.
6. **No REST API for freshness/coverage** — Only `/health/data` endpoint exists.
7. **No NAS deployment yet** — System runs locally; QNAP deployment pending.

---

## Scheduler Orchestration (R6-7B PASS)

### Component: `scheduler.py`

`run_update_cycle` orchestrates the planning → execution pipeline:

```
Coverage → Planner → Executor → JobEngine
```

Scheduler orchestrates timing only. Does not replace Planner, Executor, or Worker. Invoked on-demand; no cron or daemon loop.

---

## Module Index

| Module               | Purpose                              | Status  |
|----------------------|--------------------------------------|---------|
| `universe_builder.py`| Deterministic universe generation    | R6-1 ✅  |
| `universe.py`        | CLI entry point                      | R6-1 ✅  |
| `coverage.py`        | Coverage snapshot generation         | R6-3 ✅  |
| `freshness.py`       | Freshness state assessment           | R6-3 ✅  |
| `update_planner.py`  | Policy-driven update planning        | R6-5 ✅  |
| `update_executor.py` | Plan → JobEngine bridge              | R6-6 ✅  |
| `reconcile.py`       | Historical data import               | R6-4 ✅  |
| `data_quality.py`    | Data quality validation              | R6-9 ✅  |
| `scheduler.py`       | Update pipeline orchestration        | R6-7B ✅ |
| `scheduler_state.py` | Persistent scheduler orchestration state | R6-7B ✅ |
| `run_lifecycle.py`   | Run lifecycle management             | R7 ✅    |
| `run_verifier.py`    | Run verification                     | R7 ✅    |
| `failure_policy.py`  | Failure policy                       | R7 ✅    |
| `burnin.py`          | Burn-in validation                   | R8 ✅    |
| `operation_report.py`| Operation report                     | R8 ✅    |
| `dataset_store.py`   | DuckDB market data storage           | R5 ✅    |
| `job_engine.py`      | SQLite job lifecycle management      | R5 ✅    |
| `job_handlers.py`    | Job execution handlers               | R5 ✅    |
| `source_governor.py` | Provider routing and fallback        | R5 ✅    |
| `source_adapters.py` | Data source abstraction              | R5 ✅    |
| `health.py`          | Health check endpoints               | R6-3 ✅  |

---

## Test Coverage

| Test File                      | Purpose                          | Status   |
|-------------------------------|----------------------------------|----------|
| `test_universe_builder.py`    | Universe generation & identity   | PASS     |
| `test_coverage_freshness.py`  | Coverage snapshot & freshness    | PASS     |
| `test_update_planner.py`      | Planning logic & determinism     | PASS     |
| `test_update_executor.py`     | Job materialization & dedup      | PASS     |
| `test_reconciliation.py`      | Historical import idempotency    | PASS     |
| `test_market_bars_update.py`  | Incremental update engine        | PASS     |
| `test_job_engine.py`          | Job lifecycle & crash recovery   | PASS     |
| `test_dataset_store.py`       | DuckDB storage operations        | PASS     |

**Total:** 617 passed, 2 skipped (known: Docker-only tests)
