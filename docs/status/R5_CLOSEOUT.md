# R5 Market Data Pipeline — Closeout Report

**Status:** COMPLETE  
**Date:** 2026-08-20  
**Branch:** phase9.3-dataset-foundation

---

## A. Current Capabilities

### Durable Background JobEngine
- Persistent job queue with SQLite-backed state management
- Retry/checkpoint/restart/recovery semantics (R5-C3)
- Crash-safe transactions: BEGIN → UPSERT → COMMIT
- Idempotent chunk execution with at-least-once semantics

### Equity Daily Market Bars
- Full production burn-in validated (R5-C4A)
- 5215 instruments processed, 5209 completed (≈99.885% coverage)
- mootdx source adapter with automatic fallback chain

### Canonical Instrument Identity (R5-C4B)
- `Instrument` model as single source of truth for identity
- `canonical_id = EXCHANGE:CODE` format (e.g., `SSE:600519`, `SZSE:000001`)
- Exchange-aware chunk identity: `market_bars|SSE:000001|daily|200`
- Backward compatibility preserved for legacy bare-symbol API

### DatasetStore Explicit Exchange Identity (R5-C4C-2)
- `make_security_id(code, exchange)` validates against whitelist `{SSE, SZSE, BSE}`
- Explicit exchange takes precedence over prefix inference
- Legacy `exchange=None` path unchanged (prefix inference still works)
- Invalid exchange values raise `ValueError` immediately

### INDEX Pipeline (R5-C4C-3)
- Structured `instruments[]` input with explicit code/exchange/asset_type
- mootdx `index_bars()` routing for INDEX asset type
- Real end-to-end retrieval validated against production API
- Cross-market coexistence: `SSE:000001` INDEX and `SZSE:000001` EQUITY coexist without conflict

---

## B. Equity Production Evidence (R5-C4A)

| Metric | Value |
|--------|-------|
| Total instruments | 5215 |
| Completed (DONE) | 5209 |
| Failed | 6 |
| Coverage | ≈99.885% |

**Failed symbols (upstream/source availability exceptions):**
- 688835, 688836, 601123, 301655, 301688, 301697

All failures classified as upstream data unavailability, not architecture defects.

---

## C. INDEX Production Evidence (R5-C4C-4)

**Validation set:** 7 representative instruments across SSE and SZSE

| Instrument | Status | Rows | Source |
|------------|--------|------|--------|
| SSE:000001 | DONE | 200 | mootdx |
| SSE:000300 | DONE | 200 | mootdx |
| SSE:000688 | DONE | 200 | mootdx |
| SSE:589000 | UPSTREAM_NO_DATA | 0 | mootdx+baidu empty |
| SZSE:399001 | DONE | 200 | mootdx |
| SZSE:399006 | DONE | 200 | mootdx |
| SZSE:399300 | DONE | 200 | mootdx |

**Results:**
- Architecture failures: 0
- Duplicate rows: 0
- Cross-market contamination: 0

**SSE:589000 note:** Classified as UPSTREAM_NO_DATA. Both mootdx and baidu returned empty results. This is an upstream coverage gap, not a pipeline defect. The instrument identity (SSE:589000 INDEX) is valid and correctly processed through the pipeline.

---

## D. Frozen Identity Contracts

### Canonical ID Format
```
canonical_id = EXCHANGE:CODE
```

**Examples:**
- `SSE:600519` (贵州茅台 EQUITY)
- `SZSE:000001` (平安银行 EQUITY)
- `SSE:000001` (上证指数 INDEX)

### Rules
1. **INDEX requires explicit exchange** — bare `000001` with `asset_type=INDEX` must reject unless exchange is specified
2. **EQUITY defaults to prefix inference** — bare `600519` → `SSE:600519` (unchanged legacy behavior)
3. **Ambiguous bare identity must not guess exchange** — codes like `000001` that exist on both exchanges require explicit disambiguation for INDEX
4. **Old persisted chunks/results remain immutable** — no migration of historical data

---

## E. Deprecated Historical Artifact

### OLD_INDEX_UNIVERSE_167 = DEPRECATED_NON_REPRODUCIBLE

**Reason:** The original C4C-1 index universe (SSE=95, SZSE=72, TOTAL=167) was generated without persisting the complete code list or classification logic. Attempts to reconstruct it from TDX security master data failed because:
- Simple prefix matching yields far more codes than the original (856 vs 98 for SSE, 354 vs 72 for SZSE)
- The original classification likely used numeric cutoffs, name filtering, or other selective criteria that are no longer available
- No deterministic hypothesis could reproduce the exact 167 count

**Decision:** Do not use 167 as authoritative production coverage denominator. Future work should establish a new reproducible universe discovery pipeline (Universe v2).

---

## F. Current Test Baseline

| Metric | Value |
|--------|-------|
| Passed | 445 |
| Skipped (known) | 2 |
| New skips | 0 |

---

## G. Next Roadmap

1. **Reproducible Instrument Universe v2** — Establish deterministic, documented universe discovery
2. **Incremental daily updates** — Automated refresh pipeline for market data
3. **Coverage/freshness/gap detection** — Monitor data completeness and staleness
4. **Query/consumption API** — Read endpoints for downstream consumers
5. **NAS production release** — Deploy to QNAP Container Station
6. **Optional provider fallback** — Add alternative sources only when evidence requires it
7. **New asset types** — ETF, bonds, etc., only when business need is established

---

## Key Commits

| Phase | Commit | Description |
|-------|--------|-------------|
| C4B-1 | 50df1b2 | Identity Primitives |
| C4B-2 | 4d3fe55 | Adapter Routing |
| C4B-3 | 90cf9b1 | Persistence/Chunk Identity |
| C4B-4 | 80da9ff | Backward Compatibility |
| C4C-2 | 6f7f496 | DatasetStore Explicit Exchange Identity |
| C4C-3 | b3acd0c | End-to-end Index Market Bars Support |
