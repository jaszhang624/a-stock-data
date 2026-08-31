# Phase 9.3 — Dataset Foundation Architecture Proposal (Revision 3)

## Status: DRAFT R3 (design only, no production changes)

## Changes from R2
- Security Master: versioned snapshots with STAGING→VALIDATED→ACTIVE state machine (no destructive replace)
- Canonical `security_id = "<exchange>:<code>"` as primary identity across all tables
- market_bars_daily: PK = `(security_id, trade_date)`, no redundant `frequency` column
- Crash-safe transaction ordering: DB commit before chunk DONE
- Rollback: snapshot rollback for Security Master; idempotent retry + artifact replay for Market Bars
- Quality gates for snapshot activation (coverage, duplicates, delta analysis)

---

## 1. DuckDB DDL Proposal

```sql
-- ============================================================
-- SECURITY MASTER (versioned snapshots)
-- ============================================================

CREATE TABLE security_master_snapshots (
    snapshot_id TEXT PRIMARY KEY,       -- UUID v4
    source TEXT NOT NULL,               -- 'mootdx' / 'eastmoney' — provenance
    as_of TEXT NOT NULL,                -- YYYY-MM-DD — snapshot date
    row_count INTEGER NOT NULL,         -- total rows in this snapshot
    checksum TEXT NOT NULL,             -- SHA256 of sorted (exchange, code) pairs
    status TEXT NOT NULL DEFAULT 'STAGING',  -- STAGING / VALIDATED / ACTIVE / REJECTED
    created_at TEXT NOT NULL            -- ISO 8601 timestamp
);

CREATE TABLE security_master (
    snapshot_id TEXT NOT NULL,          -- references security_master_snapshots.snapshot_id
    security_id TEXT NOT NULL,          -- canonical: "<exchange>:<code>" e.g. "SSE:600519"
    code TEXT NOT NULL,                 -- 6-digit code: '600519'
    exchange TEXT NOT NULL,             -- 'SSE' / 'SZSE' / 'BSE'
    security_type TEXT NOT NULL,        -- 'stock' / 'etf' / 'index' / 'bond'
    board TEXT,                         -- 'main' / 'gem' / 'sci_tech' / 'bse_innovation'
    name TEXT NOT NULL,                 -- stock name (Chinese)
    listing_status TEXT DEFAULT 'active', -- 'active' / 'suspended' / 'delisted'
    list_date TEXT,                     -- YYYY-MM-DD or NULL
    delist_date TEXT,                   -- YYYY-MM-DD or NULL (NULL = active)
    source TEXT NOT NULL,               -- 'mootdx' / 'eastmoney' — provenance
    as_of TEXT NOT NULL,                -- YYYY-MM-DD — snapshot date
    FOREIGN KEY (snapshot_id) REFERENCES security_master_snapshots(snapshot_id)
);

CREATE INDEX idx_security_master_code ON security_master(code);
CREATE INDEX idx_security_master_active ON security_master(snapshot_id, code)
    WHERE snapshot_id IN (SELECT snapshot_id FROM dataset_heads WHERE status = 'ACTIVE');

-- ============================================================
-- MARKET BARS DAILY (canonical time-series)
-- ============================================================

CREATE TABLE market_bars_daily (
    security_id TEXT NOT NULL,          -- canonical: "<exchange>:<code>" e.g. "SSE:600519"
    trade_date DATE NOT NULL,           -- YYYY-MM-DD
    open DOUBLE,                        -- opening price (yuan)
    high DOUBLE,                        -- daily high (yuan)
    low DOUBLE,                         -- daily low (yuan)
    close DOUBLE,                       -- closing price (yuan)
    volume BIGINT NOT NULL DEFAULT 0,   -- shares traded (canonical unit: individual shares)
    amount DOUBLE NOT NULL DEFAULT 0,   -- yuan traded (canonical unit: yuan)
    source TEXT NOT NULL,               -- 'mootdx' / 'baidu' — provenance
    ingested_at TEXT NOT NULL,          -- ISO 8601 timestamp
    job_id TEXT,                        -- references astock_jobs.job_id (execution artifact)
    PRIMARY KEY (security_id, trade_date)
);

CREATE INDEX idx_market_bars_symbol ON market_bars_daily(security_id, trade_date DESC);

-- ============================================================
-- DATASET HEADS (active snapshot pointer)
-- ============================================================

CREATE TABLE dataset_heads (
    dataset_name TEXT PRIMARY KEY,      -- 'security_master' / 'market_bars_daily'
    active_snapshot_id TEXT NOT NULL,   -- references security_master_snapshots.snapshot_id
    status TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE / STALE
    updated_at TEXT NOT NULL            -- ISO 8601 timestamp
);

-- ============================================================
-- INGESTION LOG (audit trail)
-- ============================================================

CREATE TABLE ingestion_log (
    log_id TEXT PRIMARY KEY,            -- UUID v4
    job_type TEXT NOT NULL,             -- 'security_master_snapshot' / 'market_bars_sync'
    job_id TEXT NOT NULL,               -- references astock_jobs.job_id
    table_name TEXT NOT NULL,           -- 'security_master' / 'market_bars_daily'
    rows_inserted INTEGER NOT NULL DEFAULT 0,
    rows_updated INTEGER NOT NULL DEFAULT 0,
    rows_unchanged INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,           -- ISO 8601
    completed_at TEXT NOT NULL,         -- ISO 8601
    status TEXT NOT NULL                -- 'success' / 'partial' / 'failed'
);
```

---

## 2. Canonical Identity: `security_id`

### Definition
```
security_id = "<exchange>:<code>"
```

| Example | security_id |
|---------|-------------|
| 贵州茅台 (600519) | `SSE:600519` |
| 平安银行 (000001) | `SZSE:000001` |
| 宁德时代 (300750) | `SZSE:300750` |

### Rationale
- **Stable natural key**: Combines exchange + code, which is the actual market identifier
- **No surrogate dependency**: Doesn't require auto-increment or UUID
- **Human readable**: `SSE:600519` is self-documenting
- **Cross-table consistency**: Same identity in security_master and market_bars_daily

### Volume / Amount Canonical Units
| Field | Unit | Notes |
|-------|------|-------|
| `volume` | Individual shares (股) | NOT 手/lot. mootdx returns 股 directly; baidu may need normalization |
| `amount` | Yuan (元) | NOT 万元. Normalize from upstream if needed |

---

## 3. Snapshot State Machine (Security Master)

```
                    ┌─────────────┐
  fetch success     │             │    validation pass
  ─────────────────►│   STAGING   │─────────────────────┐
                    │             │                      │
                    └──────┬──────┘                      ▼
                           │                     ┌─────────────┐
                           │ validation fail     │             │
                           │ or quality gate     │ VALIDATED   │  activation
                           │ failure             │             │──────────────┐
                           └────────────────────►│             │              │
                                                 └──────┬──────┘              │
                                                        │                     ▼
                                                        │ rejected            ┌─────────┐
                                                        │         ┌──────────►│  ACTIVE │
                                                        │         │           └─────────┘
                                                        ▼         │
                                                   ┌──────────┐   │
                                                   │ REJECTED │◄──┘
                                                   └──────────┘
```

### State Transitions
| From | To | Trigger | Reversible? |
|------|-----|---------|-------------|
| — | STAGING | fetch completes, rows written with new snapshot_id | Yes (delete staging rows) |
| STAGING | VALIDATED | Quality gates pass | No (must re-fetch for new snapshot) |
| STAGING | REJECTED | Quality gates fail | N/A (staging discarded) |
| VALIDATED | ACTIVE | Atomic activation (update dataset_heads) | Yes (rollback to previous ACTIVE) |
| VALIDATED | REJECTED | Manual rejection or post-validation failure | N/A |

### Quality Gates (STAGING → VALIDATED)
| Gate | Check | Threshold | Action on failure |
|------|-------|-----------|-------------------|
| Unique constraint | `COUNT(DISTINCT (exchange, code)) = row_count` | 100% unique | REJECTED |
| Total coverage | `row_count >= 4500` | ≥ 4500 stocks (A-share baseline) | SUSPECT — flag for review |
| SSE coverage | `COUNT WHERE exchange='SSE' >= 1800` | ≥ 1800 | SUSPECT |
| SZSE coverage | `COUNT WHERE exchange='SZSE' >= 2500` | ≥ 2500 | SUSPECT |
| BSE coverage | `COUNT WHERE exchange='BSE' >= 200` | ≥ 200 | SUSPECT (newer exchange, lower threshold) |
| Security type distribution | `security_type IN ('stock','etf','index','bond')` | All types present | SUSPECT if missing expected type |
| Duplicate rate | `COUNT(*) - COUNT(DISTINCT code) = 0` | 0 duplicates | REJECTED if > 0 |
| Delta vs previous ACTIVE | `abs(new_count - prev_count) / prev_count < 0.3` | ≤ 30% change | SUSPECT if > 30% (may indicate partial fetch) |

---

## 4. market_bars_sync Transaction Sequence

### Crash-Safe Ordering
```
1. claim chunk (Job Engine)
   → chunk status: PENDING → RUNNING

2. acquire data via Source Governor
   → bars = governor.fetch_market_bars(symbol, 'daily', count)

3. normalize to canonical format
   → security_id = derive from symbol (e.g., '600519' → 'SSE:600519')
   → normalize volume/amount units

4. atomic artifact write (Job Engine)
   → write /data/jobs/{job_id}.json

5. DuckDB BEGIN (transaction start)
   → UPSERT INTO market_bars_daily ... ON CONFLICT DO UPDATE

6. DuckDB COMMIT
   → canonical data persisted

7. mark chunk DONE (Job Engine)
   → chunk status: RUNNING → DONE

8. update job status (if all chunks done)
   → job status: RUNNING → DONE
```

### Critical Invariants
| Rule | Enforcement |
|------|-------------|
| DB commit MUST succeed before chunk DONE | Chunk DONE is step 7, after COMMIT (step 6) |
| Retry safety: if crash between steps 5-7, chunk stays RUNNING → RETRY | Job Engine crash recovery: RUNNING chunks → RETRY on restart |
| Idempotent retry: re-running UPSERT is safe (ON CONFLICT DO UPDATE) | DuckDB upsert semantics guarantee idempotency |
| Chunk NEVER marked DONE before canonical commit | Code structure enforces ordering (step 7 after step 6) |

### Crash Scenarios
| Crash Point | Recovery Behavior | Data Integrity |
|-------------|-------------------|----------------|
| Step 1-4 (before DB) | Chunk RETRY on restart | No partial writes |
| Step 5-6 (during transaction) | DuckDB auto-rollback; chunk RETRY | Transaction atomicity guarantees no partial state |
| Step 6-7 (after commit, before DONE) | Chunk RETRY; UPSERT is idempotent → no duplicate data | Safe: retry re-upserts same data (idempotent) |
| Step 7-8 (after chunk DONE) | Job continues with next chunk | Chunk already complete |

---

## 5. First Initialization Sequence

```
Phase 1: Security Master (one-time)
─────────────────────────────────────
1. Submit security_master_snapshot job for each source (mootdx, eastmoney)
2. Handler fetches full security list → writes to staging snapshot_id
3. Quality gates run automatically on STAGING data
4. If VALIDATED → atomic activation (update dataset_heads)
5. If REJECTED → log failure, do NOT activate

Phase 2: Market Bars (batched)
───────────────────────────────
6. Read active security_master snapshot for symbol list
7. Batch symbols into groups of 30-50 (conservative)
8. For each batch:
   a. Submit market_bars_sync job (symbols=[...], count=800)
   b. Job Engine creates chunks (one per symbol)
   c. Worker processes chunks sequentially
   d. Each chunk: acquire → normalize → DuckDB upsert → DONE
9. Wait for batch completion before next batch
10. Repeat until all symbols processed

Phase 3: Verification
─────────────────────
11. Check job completion rates
12. Verify row counts in market_bars_daily vs expected
13. Log any failed symbols for manual review
```

---

## 6. Daily Incremental Sequence

```
Daily Job Schedule (recommended):
─────────────────────────────────
06:00 security_master_snapshot (low priority, full snapshot)
07:00 market_bars_sync batch 1-4 (core stocks, count=5)
12:00 market_bars_sync batch 5-8 (remaining stocks, count=5)

Each day:
1. security_master_snapshot fetches latest universe
   → STAGING snapshot with quality gates
   → If VALIDATED, activates as new ACTIVE snapshot

2. market_bars_sync fetches latest N bars per symbol
   → Upsert by (security_id, trade_date) — only new/changed bars written
   → Idempotent: safe to re-run with same parameters

3. Job Engine handles retry for failed chunks
   → Transient errors: automatic retry with backoff
   → Permanent errors: chunk FAILED, logged for review
```

---

## 7. Failure / Retry Matrix

| Scenario | Detection | Recovery | Data Impact |
|----------|-----------|----------|-------------|
| Source Governor OPEN (circuit breaker) | HTTP 503 / TransientJobError | Job WAITING_SOURCE → retry after cooldown | No data loss; chunk retries later |
| DuckDB transaction failure | Exception during UPSERT | Transaction auto-rollback; chunk RETRY | No partial writes (ACID) |
| Chunk crash between commit and DONE | RUNNING chunk on restart | RETRY → idempotent UPSERT re-runs | No duplicate data (upsert semantics) |
| Quality gate failure (Security Master) | STAGING snapshot fails validation | Snapshot marked REJECTED; previous ACTIVE unchanged | No data loss; old snapshot preserved |
| Full job failure (all chunks fail) | Job status → FAILED | Manual review + re-submit with adjusted params | No data loss; job artifact preserved |
| NAS disk full | DuckDB write error → exception | Chunk RETRY; alert for manual intervention | May lose in-flight bars if disk not freed |

---

## 8. Rollback / Recovery Boundaries

### Security Master
| Action | Mechanism | Scope |
|--------|-----------|-------|
| Snapshot rollback | Update `dataset_heads.active_snapshot_id` to previous ACTIVE snapshot_id | Instant, atomic |
| Reject current ACTIVE | Mark as STALE; activate previous snapshot | Requires manual approval |
| Rebuild from scratch | Delete all security_master rows; re-run snapshot job | Destructive — requires full re-fetch |

### Market Bars
| Action | Mechanism | Scope |
|--------|-----------|-------|
| Idempotent retry | Re-run market_bars_sync job with same params | Safe: UPSERT overwrites existing rows |
| Artifact-based replay | Re-read `/data/jobs/{job_id}.json` and re-ingest | Requires job artifact to exist |
| Full rebuild | Delete market_bars_daily rows for specific symbol; re-run sync | Destructive per-symbol — requires upstream fetch |
| **NOT supported** | Per-ingestion row-level rollback | Out of scope for Phase 9.3 |

---

## 9. NAS Directory Layout

```
/share/Docker/a-stock-data/
├── .env                              # Environment config (unchanged)
├── data/
│   ├── astock_jobs.db                # Job Engine — control plane (unchanged)
│   ├── astock_source_governor.db     # Source Governor — circuit breaker state (unchanged)
│   ├── astock_data.duckdb            # Dataset Store — canonical data plane (NEW)
│   └── jobs/                         # Job execution artifacts (unchanged)
│       ├── {job_id}.json             # Per-job result artifact
│       └── ...
├── cache/                            # Runtime cache (unchanged)
│   └── ...
└── deploy/                           # Deployment artifacts (unchanged)
```

---

## 10. Phase 9.3 Scope Summary (R3)

### In Scope
| Item | Status | Notes |
|------|--------|-------|
| DuckDB DDL (final) | ✅ R3 complete | security_master, market_bars_daily, dataset_heads, ingestion_log |
| Snapshot state machine | ✅ R3 complete | STAGING → VALIDATED → ACTIVE with quality gates |
| Canonical security_id | ✅ R3 complete | `"<exchange>:<code>"` format |
| Crash-safe transaction ordering | ✅ R3 complete | DB commit before chunk DONE |
| Rollback strategy | ✅ R3 complete | Snapshot rollback (SM); idempotent retry + artifact replay (MB) |
| NAS directory layout | ✅ R3 complete | astock_data.duckdb in /data/ |
| Failure/retry matrix | ✅ R3 complete | All scenarios documented |

### Out of Scope (Phase 9.4+)
| Item | Reason |
|------|--------|
| Security Master upstream selection | Needs read-only investigation first |
| DuckDB dependency installation | Requires image rebuild (Phase 9.4) |
| lxml/html5lib for ths_eps_forecast / full_valuation | Backlog item, not blocking Phase 9.3 |
| Parquet export/archive | Future optimization (not canonical store) |
| Other 49 functions as job types | Phase 9.4+ decision |
| Per-ingestion row-level rollback for market bars | Complexity not justified at Phase 9.3 scale |

---

## 11. Known Issues (unchanged from R2)
| Function | Issue | Classification | Phase 9.3 Action |
|----------|-------|---------------|------------------|
| full_valuation | Missing lxml (via ths_eps_forecast → pd.read_html) | APPLICATION_ERROR | Skip — backlog |
| ths_eps_forecast | Missing lxml/html5lib/bs4 | APPLICATION_ERROR | Skip — backlog (Phase 9.2 known) |
| eastmoney_stock_info | HTTP 503 from upstream | UPSTREAM_BLOCKED | Retry later or skip |
| sina_financial_report (xjllb/fzbb) | AttributeError on NoneType | PARSER_ERROR | Skip these report types |
