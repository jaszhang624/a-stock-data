# Project Context — a-stock-data

**Purpose of this file:** Stable, maintainable project context for future coding sessions.
**Last verified:** 2026-08-28 (P9.4 Step 3 verified; P9.4 Step 1 `55c005d`, branch `phase9.3-dataset-foundation`).
**Companion files:** `current_architecture.md` (frozen architecture snapshot), `current_state.json` (data-state snapshot), `development_state.md` (rolling status).

---

## 1. Project Purpose

A self-contained market data collection and storage system for Chinese A-share
equities and indices. It turns raw data scattered across 15 upstream providers
(mootdx/TDX, Eastmoney, Sina, THS, Tencent, Baidu, CNInfo, CLS, …) into a
canonical, queryable dataset with:

- deterministic instrument universe generation
- coverage / freshness state tracking
- policy-driven update planning and job materialization
- crash-safe, idempotent, durable background jobs
- a FastAPI surface over the 44 upstream data functions

The system follows a strict **planning ≠ execution** separation: planners decide
*what* should happen; executors and workers decide *how*.

## 2. Architecture Overview (two-plane design)

```
Control plane (SQLite, JobEngine)          Data plane (DuckDB, DatasetStore)
  jobs, chunks, checkpoints,                canonical market_bars_daily,
  run lifecycle, scheduler runs,            security_master snapshots,
  plan materializations (dedup),            dataset_heads, ingestion_log
  source governor state

Upstream adapters → SourceGovernor (routing + fallback + circuit breaker)
                → Job handlers → DatasetStore (DuckDB)
```

- **SQLite for metadata, DuckDB for data.** Job state lives in SQLite;
  market bars and the security master live in DuckDB.
- **Identity-first:** canonical `security_id = "<EXCHANGE>:<CODE>"`
  (e.g. `SSE:600519`). Bare symbols are never used after parsing; the critical
  isolation rule is `SSE:000001 INDEX ≠ SZSE:000001 EQUITY`.

## 3. Main Modules and Responsibilities

Location: `docker_api/src/astock_api/` (36 modules) and `docker_api/tests/` (38 test files, flat layout).

### Entry points
| Module | Role |
|---|---|
| `main.py` | FastAPI app; registers all upstream functions via `registry.py`; mounts `job_routes`, `health`, `/api/v1/{meta,functions,call}` |
| `universe.py` | CLI entry for universe generation (`python -m astock_api.universe build`) |
| `scheduler.py` | `run_update_cycle()` / `run_update_cycle_from_files()` — full cycle orchestration |

### Pipeline (planning → execution)
| Module | Responsibility |
|---|---|
| `universe_builder.py` | Deterministic instrument universe v2 (reproducible SHA256 artifact `data/universe/instrument_universe_v2.json`) |
| `coverage.py` | Coverage snapshot generation (now sourced from the security master, P9.3-B) |
| `freshness.py` | Freshness state assessment |
| `update_planner.py` | `UpdatePlanner` + `FreshnessPolicy` → frozen `UpdatePlan` |
| `update_executor.py` | Plan → JobEngine bridge (`materialize_plan`), plan-hash dedup |
| `update_cycle_scheduler.py` | **P9.4 Step 1:** in-memory, time-only trigger primitive (`UpdateCycleScheduler`, `ScheduleConfig`); owns the *when*, not the *how*; driven by `update_cycle_service.py` (P9.4 Step 2) |
| `update_cycle_service.py` | **P9.4 Step 2 + Step 3:** daemon-thread driver loop that ticks the scheduler off the asyncio event loop; skip-not-wait single-flight guard; idempotent start/stop; stop before `engine.stop()`; gated by `UPDATE_CYCLE_ENABLED` (default off). **Step 3:** loads/saves the durable trigger instant in the JobEngine DB (`run_lifecycle`), so a restart does not re-trigger before the interval elapses |
| `scheduler_state.py` | Persistent `scheduler_runs` state in the JobEngine SQLite DB (RUNNING → SUCCESS/FAILED/INTERRUPTED, stale-run recovery) |
| `run_lifecycle.py` / `run_verifier.py` / `failure_policy.py` | Run lifecycle management, verification gate, failure policy (R7). **P9.4 Step 3:** the durable trigger state lives in a dedicated `update_cycle_state` table (same JobEngine DB, separate from `update_runs`) via `save_trigger_state` / `load_trigger_state` |
| `burnin.py` / `operation_report.py` | Burn-in validation and operation reporting (R8) |

### Storage and jobs
| Module | Responsibility |
|---|---|
| `dataset_store.py` | DuckDB canonical data plane: DDL, bootstrap, UPSERT, snapshot lifecycle, quality-gate constants (`GATE_*`), `rollback_snapshot()` |
| `security_master.py` | Security Master lifecycle orchestration (import → validate → activate → rollback → reject; refresh pipeline P9.3-C) |
| `security_master_handler.py` | Live TDX acquisition + equity classification (market=0→SZSE, market=1→SSE); upstream path still needs real-environment validation |
| `job_engine.py` | SQLite-backed job/chunk lifecycle: retries, checkpoints, crash recovery |
| `job_handlers.py` | Job execution handlers (incl. `market_bars_sync`, `security_master_snapshot`) |
| `data_quality.py` | Data quality validation (R6-9) |

### Source layer
| Module | Responsibility |
|---|---|
| `source_governor.py` | Provider routing, fallback chain, circuit breaker |
| `source_adapters.py` | Data source abstraction |
| `source_state_store.py` | Provider state tracking |
| `upstream/` | Raw fetchers per provider: `eastmoney`, `sina`, `tencent`, `ths`, `cls`, `cninfo`, `other`, `common` (mootdx client) |

### Support
`registry.py` (function registry), `serializer.py`, `security.py` (API key),
`config.py`, `health.py` (`/health/data`, `/health/worker`), `api/routes.py`.

## 4. Data Flow (one update cycle)

```
UpdateCycleService (P9.4 Step 2+3, update_cycle_service.py)
  daemon-thread driver loop, off the asyncio event loop
  ├─ loads durable trigger instant on construct (Step 3, run_lifecycle)
  └─ UpdateCycleScheduler (in-memory trigger, P9.4 Step 1)
        ↓ when due  →  saves durable trigger instant (Step 3, run_lifecycle, BEFORE callback)
                       → run_callback (bound in a closure)
run_update_cycle()          scheduler.py
  1. Coverage snapshot      coverage.py  (reads security master ACTIVE snapshot)
  2. Update plan            update_planner.py (freshness policy → actions)
  3. Materialize jobs       update_executor.py (plan-hash dedup vs plan_materializations)
  4. Jobs execute           job_engine.py + job_handlers.py
                             → SourceGovernor → upstream adapters
                             → DatasetStore.upsert (DuckDB, canonical security_id)
  5. Verify                 run_verifier.py (quality, coverage gates)
  6. Lifecycle bookkeeping  run_lifecycle.py / scheduler_state.py
```

Security Master update path:
```
fetch (security_master_handler) → STAGING snapshot → quality gates
  → VALIDATED (+ BSE satisfied) → ACTIVE (atomic pointer switch in dataset_heads)
  → VALIDATED but BSE missing → VALIDATED_PARTIAL (never ACTIVE)
  → invalid → REJECTED (previous ACTIVE untouched)
rollback = pointer switch back to previous ACTIVE snapshot
```

## 5. Important Design Principles

1. **Identity-first** — canonical `EXCHANGE:CODE`; identity is never reconstructed from a bare symbol downstream.
2. **Idempotent operations** — all mutations (import, upsert, job creation) are safe to re-run.
3. **No silent data loss** — backup before mutation; verify after execution.
4. **Planning ≠ Execution** — planner decisions; executor materialization; worker collection.
5. **Crash-safe ordering** — DB commit MUST precede chunk DONE; RUNNING chunks recover to RETRY on restart; UPSERT semantics make retries duplicate-free.
6. **Quality gates before activation** — snapshots are not promoted to ACTIVE until gates pass (uniqueness, per-exchange coverage, delta ≤ 30%).
7. **Reproducible artifacts** — universe build is deterministic (same input → same SHA256).
8. **In-memory triggers, persistent state** — scheduler timing may live in memory; all durable state lives in SQLite/DuckDB. **P9.4 Step 3:** the trigger instant itself is now durable (in the JobEngine `update_cycle_state` table, separate from `update_runs`), so scheduling continuity survives restart while the in-memory timing drives the loop between ticks.

## 6. Development Milestones

| Milestone | Content | Status |
|---|---|---|
| R5 | Durable JobEngine (retries/checkpoints/crash recovery), equity daily bars, canonical identity, index pipeline, explicit exchange identity | CLOSED (burn-in ≈99.885%, 5209/5215) |
| R6 | Universe v2, coverage/freshness (R6-3), reconciliation (R6-4), planner (R6-5), executor (R6-6), dedup registry (R6-7A), scheduler orchestration (R6-7B), data quality (R6-9) | ALL PASS (regression baseline 617 passed / 2 skipped) |
| R7 | Run lifecycle, verification, failure policy | PASS |
| R8 | Burn-in validation, operation report | PASS |
| P9.3-A/B/C | Security master: foundation, coverage switch, refresh pipeline | COMPLETE |
| P9.4 Step 1 | Update cycle scheduler primitive (in-memory trigger) | COMPLETE (`55c005d`) |
| P9.4 Step 2 | FastAPI lifecycle wiring (`UpdateCycleService`, `main.py`, `config.py`) | VERIFIED — 11/11 service tests; 680 passed / 2 skipped full regression. Uncommitted. |
| P9.4 Step 3 | Durable trigger-state persistence (`run_lifecycle` `update_cycle_state` table, separate from `update_runs`; `UpdateCycleService` load/save-before-callback) | VERIFIED — 19/19 trigger-state tests; 699 passed / 2 skipped full regression. Uncommitted. |

---
*This file is meant to be stable: update it only when the architecture, module layout, or milestone history changes. For day-to-day status, maintain `development_state.md` instead.*
