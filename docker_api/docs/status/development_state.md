# Development State — a-stock-data

**Purpose of this file:** Rolling, up-to-date status snapshot. Update this file when development progress changes.
**Last verified:** 2026-08-28 (P9.4 Step 3 verified; P9.4 Step 1 `55c005d`, branch `phase9.3-dataset-foundation`).
**Companion files:** `project_context.md` (stable project context), `current_architecture.md` (frozen architecture snapshot), `current_state.json` (data-state snapshot).

---

## 1. Current Phase

**Phase 9.4 — Automated Update Cycle (in progress).**

Phase 9.3 (Dataset Foundation: security master + DuckDB data plane) is complete.
Phase 9.4 has all three steps done:

- ✅ **P9.4 Step 1** — `update_cycle_scheduler.py`: a minimal, in-memory, time-only
  trigger primitive (`UpdateCycleScheduler` + `ScheduleConfig`). It owns only the
  *when* decision and the trigger call; the existing `run_update_cycle()` remains the
  executor. Committed as `55c005d` (tag `p9-4-step1-update-cycle-trigger-pass`).
- ✅ **P9.4 Step 2** — `update_cycle_service.py` + FastAPI lifespan wiring
  (`main.py`, `config.py`): `UpdateCycleService` drives `UpdateCycleScheduler` from
  a daemon-thread loop off the asyncio event loop, with a skip-not-wait single-flight
  guard, idempotent start/stop, and stop-before-engine teardown ordering. Config knobs
  `UPDATE_CYCLE_ENABLED` (default **off**), `UPDATE_CYCLE_INTERVAL_MIN` (1440), and
  `UPDATE_CYCLE_TICK_SECONDS` (60). Verified: 11/11 service tests pass. **Uncommitted.**
- ✅ **P9.4 Step 3** — trigger-state persistence: the trigger instant now survives
  service restarts. `UpdateCycleService` loads it from the JobEngine DB (`run_lifecycle`
  `update_cycle_state` table, a dedicated singleton row separate from `update_runs`) on
  construction and saves it *before* the cycle callback begins (so a crash mid-cycle
  cannot lose the trigger). A restart before the interval no longer re-triggers; after
  the interval it becomes due again. First-run / disabled / corrupt-state / write-failure
  semantics are preserved. Verified: 19/19 trigger-state tests pass. **Uncommitted.**

## 2. Current Status

- **Working tree:** P9.4 Step 2 + Step 3 changes are uncommitted and verified —
  `config.py` (+3 knobs: `UPDATE_CYCLE_ENABLED`/`UPDATE_CYCLE_INTERVAL_MIN`/`UPDATE_CYCLE_TICK_SECONDS`),
  `main.py` (lifespan wiring), `update_cycle_service.py` (new, driver loop + Step 3
  load/save-before-callback), `run_lifecycle.py` (+`update_cycle_state` dedicated
  table, `save_trigger_state` / `load_trigger_state`), `tests/test_update_cycle_service.py`
  (11 tests), `tests/test_update_cycle_trigger_state.py` (19 tests).
- **Regression baseline:** 699 passed, 2 skipped (Docker-only tests) with the P9.4
  Step 2 + Step 3 changes applied (up from 691 at Step 3's first pass and 680 at
  Step 2).
- **Data state** (`current_state.json`, 2026-08-23): 5,210 instruments / 1,035,379 rows
  in DatasetStore; coverage: 5,210 with data, 14 missing, 1 current, 5,209 stale.
  (`sqlite_user_version: 3`.)
- **Security master:** snapshot lifecycle (STAGING → VALIDATED → ACTIVE with quality
  gates, rollback, rejection, idempotency, BSE-gated promotion) implemented and
  covered by tests; coverage read path switched to the security master source (P9.3-B);
  refresh pipeline primitive added (P9.3-C).

## 3. Pending Work / Open Items

### Phase 9.4 — Steps 2 + 3 done

**P9.4 Step 2 (uncommitted):** `update_cycle_service.py` wires `UpdateCycleScheduler`
into the FastAPI lifespan in `main.py` — when `UPDATE_CYCLE_ENABLED`, a `DatasetStore`
is opened, the `UpdateCycleService` is constructed with `run_callback = run_update_cycle`
(bound via closure), and started on a daemon thread. On teardown, `service.stop()` is
called **before** `engine.stop()`. The driver loop ticks every `UPDATE_CYCLE_TICK_SECONDS`
and calls `scheduler.run_once()`; the single-flight guard skips (rather than waits) when
a cycle is already in flight. The feature defaults to **off** (`UPDATE_CYCLE_ENABLED=false`).
See `update_cycle_scheduler_analysis.md` §4 for the original plan.

**P9.4 Step 3 (uncommitted):** trigger-state persistence. The scheduler's trigger
instant was previously process-local (in-memory `next_due`); a restart reset it to
"never triggered" so the first tick after boot re-fired a cycle. Step 3 persists it in
the JobEngine SQLite DB — in a **dedicated `update_cycle_state` table** (same DB as the
run lifecycle, but a separate table so `update_runs` holds only real lifecycle runs and
generic readers never surface scheduler metadata). `UpdateCycleService` loads it on
construction (so a restart before the interval does NOT re-trigger) and saves it
*before* the cycle callback begins (inside the single-flight guard), so at most one
durable write accompanies one accepted trigger and a crash mid-cycle cannot lose it.
The plan-hash dedup in the cycle remains the separate, cache-independent idempotency
layer. Failure policy: a missing state (first run), a corrupt timestamp, a read
failure, or a write failure all degrade gracefully (first-run semantics / in-memory
timing only) and never break the service or the cycle. If the durable write fails,
the in-memory schedule stays advanced for this process and the limitation is made
explicit (restart continuity for that trigger cannot be guaranteed). Verified by
19 targeted tests.

Planned follow-ups:
1. Integration tests for the wired trigger in a real service context with a real
   (or fixture) store — the Step 2/Step 3 tests cover the service, wiring, and durable
   state with stubbed dependencies and a real `JobEngine` DB; an end-to-end cycle with a
   real DatasetStore remains.

### Known limitations (from `current_architecture.md` "Current Limitations")
1. **Scheduler timing is manual** — `run_update_cycle` is invoked on-demand; no cron/daemon loop. (P9.4 Step 1 supplies the trigger; Step 2 wires it into the service and Step 3 makes the trigger timing durable — all verified, uncommitted.)
2. **No automatic freshness refresh** — coverage snapshots are generated on-demand.
3. **BSE unsupported** — TDX source unreliable for Beijing Stock Exchange instruments; the BSE official API requires a browser session (audit: redirect loop). Snapshots lacking BSE land in VALIDATED_PARTIAL, never ACTIVE.
4. **INDEX limited to 7 validated seeds** — full INDEX universe not generated (legacy 167-instrument index universe is deprecated as non-reproducible).
5. **Single worker thread** — JobEngine processes jobs sequentially.
6. **No REST API for freshness/coverage** — only `/health/data` exposes this data.
7. **No NAS deployment yet** — system runs locally.
8. **Backlog (Phase 9.4+ decisions):** `lxml`/`html5lib` for `ths_eps_forecast` / `full_valuation`; Parquet export/archive; exposing the other 49 upstream functions as job types.

## 4. Verification Notes

- Phase 9.3 work was validated with targeted unit tests (lifecycle: 21 tests; read path;
  refresh pipeline: 17 tests, full security master regression 67 passed) plus the
  standing regression suite.
- Security master **live upstream acquisition** (TDX/eastmoney enumeration in
  `security_master_handler.py`) is still to be validated in a real environment —
  earlier in-container regression runs were blocked by a PyPI network timeout
  (mootdx download), recorded as an infrastructure issue, not a code issue.
- QNAP/NAS deployment requires explicit authorization and has not been performed.

## 5. Recent Milestones (chronological, latest last)

- **R5** — Market data pipeline: durable JobEngine (R5-C1 retry terminalization, R5-C2 checkpoints, R5-C3 crash recovery), equity daily bars burn-in (5209/5215 ≈ 99.885%), canonical identity `EXCHANGE:CODE`, explicit exchange in DatasetStore, INDEX pipeline (R5-C4C-3). CLOSED 2026-08-20.
- **R6** — Universe v2 (reproducible, 5224 instruments); coverage/freshness (R6-3); reconciliation of 5,209 instruments / 1,035,279 rows (R6-4); freshness-driven planner (R6-5); update plan executor (R6-6); plan materialization dedup registry (R6-7A); persistent scheduler orchestration state (R6-7B); data quality validation (R6-9). ALL PASS.
- **R7** — Run lifecycle verification and failure policy. PASS.
- **R8** — Burn-in validation and operation report. PASS.
- **P9.3-A / B / C** — Security master: foundation, coverage switch, refresh pipeline. COMPLETE.
- **P9.4 Step 1** — Update cycle scheduler primitive. COMPLETE (`55c005d`).
- **P9.4 Step 2** — FastAPI lifecycle wiring (`UpdateCycleService`, `main.py`, `config.py`).
  VERIFIED (11/11 service tests; 680 passed / 2 skipped full regression). Uncommitted.
- **P9.4 Step 3** — Durable trigger-state persistence (`run_lifecycle` `update_cycle_state`
  table, separate from `update_runs`; `UpdateCycleService` load/save-before-callback).
  VERIFIED (19/19 trigger-state tests; 699 passed / 2 skipped full regression). Uncommitted.

## 6. Planned Future Work

1. End-to-end update-cycle integration test with a real (or fixture) DatasetStore —
   the Step 2/Step 3 tests cover wiring + durable state with stubbed dependencies and a
   real `JobEngine` DB, but a full cycle against a live store remains.
2. Validate security master live upstream acquisition against real TDX/eastmoney endpoints and promote a production snapshot through the quality gates.
3. Run the full regression suite in the Docker container once the PyPI network issue is resolved.
4. Extend INDEX universe beyond the 7 validated seeds (only if business need is established).
5. Evaluate BSE coverage via an alternative source (official site requires browser session; TDX path unsupported).
6. Optional: freshness/coverage REST endpoints; NAS/QNAP production deployment (requires explicit authorization).

---
*Keep this file current: update §1–§4 when phase work starts, blocks, or completes, and add a row to §5 for each new milestone.*
