# Task Plan: Phase 9.3 Dataset Foundation Implementation

## Status: IMPLEMENTATION COMPLETE — awaiting independent review

## Approved Architecture (R3)
- Proposal: `.hermes/plans/phase9.3-proposal.md`
- Branch: `phase9.3-dataset-foundation` (from tag `phase9.2-r2-production`)

## Implementation Summary
| Component | Status | Notes |
|-----------|--------|-------|
| dataset_store.py | ✅ DONE | DuckDB canonical data plane (DDL, bootstrap, UPSERT, snapshots, validation) |
| security_master_handler.py | ✅ DONE | Acquisition + validation pipeline (upstream TBD) |
| job_engine.py | ✅ MODIFIED | Added security_master_snapshot + market_bars_sync job types |
| job_handlers.py | ✅ MODIFIED | Added market_bars_sync handler (Source Governor + DuckDB) |
| requirements.txt / .in | ✅ MODIFIED | Added duckdb>=1.0,<2 |
| tests/test_dataset_store.py | ✅ DONE | 20/20 PASS (local Python 3.9) |

## Test Results
- `tests/test_dataset_store.py`: **20/20 PASS**
  - DDL/bootstrap idempotency ✅
  - security_id normalization (SSE/SZSE/BSE) ✅
  - Market bars UPSERT idempotency ✅
  - Security master snapshot lifecycle (STAGING→VALIDATED→ACTIVE) ✅
  - Failed snapshot does not replace ACTIVE ✅
  - Empty/suspicious universe cannot activate ✅
  - Path traversal prevention ✅
  - Concurrent writer safety ✅
- Existing regression tests: **BLOCKED** by Docker network timeout (mootdx PyPI download fails)
  - This is an infrastructure issue, not a code issue

## Known Limitations
1. Security master upstream acquisition is placeholder — mootdx/Eastmoney enumeration needs real testing
2. Docker build blocked by PyPI network timeout — full regression suite cannot run in container
3. Existing market_bars_snapshot handler unchanged (regression preserved by code review)

## Proposed Image Tag
`a-stock-data-api:281fc69-phase9-3-r1-dev`

## Next Steps (after review)
1. Fix Docker network issue and run full regression suite in container
2. Test mootdx/Eastmoney security enumeration on production
3. Build and deploy to QNAP (requires explicit authorization)
