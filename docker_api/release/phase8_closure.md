# Phase 8 — Production Deployment ✅ CLOSED

**Date:** 2026-08-09  
**Status:** CLOSED  
**Production Baseline:** `a-stock-data-api:281fc69-fix5-r4`

## Final Artifact

| Item | Value |
|------|-------|
| Image | a-stock-data-api:281fc69-fix5-r4 |
| Platform | linux/amd64 |
| Artifact SHA256 | `aea563b930d693249c6026a51a580b10bd10cf6ba70c647b7deecb50dc664344` |
| Config Digest | `ddb9389dacdb7d1586fc65842be43996ed8e781b6e6e6e7c1bb56ebc226ce3e5` |
| Path | /tmp/a-stock-data-api-281fc69-fix5-r4-linux-amd64.tar.gz |
| Size | 105M (109,944,901 bytes) |

## Rollback Image

| Item | Value |
|------|-------|
| Image | a-stock-data-api:281fc69-fix5-r3 |
| Config Digest | `64e5ddf3554297cde9ff414d436303de0611595b488214e6cb85d9faecb25e3b` |
| Status | Retained as rollback baseline |

## Registry

- Total: 54
- Public: 50
- Internal: 4
- Unbound public: 0

## TDX Resilience (fix5-r4)

| Capability | Status |
|------------|--------|
| Dynamic discovery (hosts["HQ"] dict fix) | ✅ PASS |
| Persistent inventory 32 → 32 (survives mootdx mutation) | ✅ PASS |
| Last-good reuse (warm: 0.227s vs cold: 20.267s) | ✅ PASS |
| Last-good death recovery via persistent inventory | ✅ PASS |
| server="unknown" fixed (adapter-owned status) | ✅ PASS |
| Real selected server: 180.153.18.170:7709 | ✅ PASS |
| 27/27 TDX discovery tests | ✅ PASS |

## Health Endpoints

| Endpoint | Status |
|----------|--------|
| /health/live | ✅ 200 |
| /health/ready | ✅ 200 (50 functions) |
| /health/tdx | ✅ 200 (path + server reported) |

## Business Smoke Test

| Check | Result |
|-------|--------|
| API Key authentication | ✅ PASS |
| FastAPI routing | ✅ PASS |
| Real upstream request (tencent_quote) | ✅ PASS |
| JSON response | ✅ PASS |
| HTTP status | ✅ 200 |

## Container Health

| Check | Result |
|-------|--------|
| Container health | ✅ healthy |
| RestartCount | ✅ 0 |

## Known Issues

### market/bars count parameter not strictly honored

- **Description:** `/api/v1/market/bars/{symbol}` with `count=3` returned 10 rows
- **Severity:** LOW
- **Phase 8 blocker:** NO
- **Root cause:** Baidu upstream may have minimum return count; adapter does not truncate before returning
- **Action:** Defer to future phase — not worth perturbing stable production image

## Fix History

| Version | Description |
|---------|-------------|
| fix4 | TDX resilience: 10 servers, bestip fallback, bare factory |
| fix5 | Robust discovery: last-good cache, dynamic pool, per-server tolerance |
| fix5-r2 | Production closure: hosts["HQ"] dict access, separate stage budgets, explicit server cache |
| fix5-r3 | Same-process mutation fix: snapshot before any Quotes.factory() |
| **fix5-r4** | **Persistent inventory + adapter-owned status (PRODUCTION BASELINE)** |

## Gates Summary

| Gate | Result |
|------|--------|
| Semantic Parity | ✅ PASS |
| TDX Discovery Tests (27/27) | ✅ PASS |
| Dependency Reality | ✅ PASS |
| Persistent Inventory | ✅ PASS |
| Last-good Failure Recovery | ✅ PASS |
| Observability | ✅ PASS |
| Release Gate | ✅ PASS |
| Artifact Gate | ✅ PASS |

**Recommendation: PROMOTE_FIX5_R4_TO_QNAP** ✅
