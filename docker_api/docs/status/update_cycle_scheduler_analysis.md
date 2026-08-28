# UpdateCycleScheduler — Wiring Analysis

*Focus: architecture review Risk #2 ("built but unwired"). Scope: `update_cycle_scheduler.py`, `scheduler.py`, related tests and docs. No code modified.*

**Status (2026-08-28): §4 Steps 1–5 are implemented and verified (P9.4 Step 2), and
P9.4 Step 3 — durable trigger-state persistence — is now implemented and verified.
See the status header at the end of this document for the verification summary.**

## 1. Is it actually unwired? — **Confirmed**

- Grep across `docker_api/src/` and `docker_api/tests/`: `UpdateCycleScheduler` / `update_cycle_scheduler` appears only in the module itself and `tests/test_update_cycle_scheduler.py`. No production code imports, instantiates, or calls it.
- `main.py` `lifespan()` never constructs one; `scheduler.py` never references it; `config.py` has no related env var. Consistent with `development_state.md` §3 ("verified by grep").
- The only real caller of `run_update_cycle()` is `burnin.py` (R8), which calls it directly — bypassing the scheduler entirely.
- `tests/test_update_cycle_scheduler.py` (5 tests) exercises only the time-decision logic with stand-in `lambda`/counting callbacks; none touches the real cycle.

**Conclusion:** the primitive is complete, tested in isolation, and intentionally inert until a driver supplies it.

## 2. Intended integration point

The module docstring states the contract explicitly:

> "State is held in memory only — no database, no config files, no daemon thread yet. **The caller supplies the real cycle as `run_callback` and drives `run_once()` from whatever loop it chooses.**"

So the integration belongs in a **driver loop** (not inside `update_cycle_scheduler.py`, which is deliberately minimal and complete). Per `development_state.md` §3 (P9.4 Step 2), the planned home is:

> "Wire `UpdateCycleScheduler` into the FastAPI lifespan in `main.py` with `run_callback = run_update_cycle`, driving `run_once()` from a service loop."

### Signature mismatch — the one real design decision

`run_update_cycle(store, engine, universe_path, reference_date=None, output_dir=None)` takes **mandatory positional args**, but `UpdateCycleScheduler.run_callback` is typed `Callable[[], Any]` (zero-arg). The wiring must therefore bind the dependencies in a **closure** (or `functools.partial`):

```python
from astock_api.scheduler import run_update_cycle
from astock_api.dataset_store import DatasetStore
from astock_api.update_cycle_scheduler import UpdateCycleScheduler, ScheduleConfig

store = DatasetStore("/app/data/astock_data.duckdb")
engine = app.state.job_engine  # created in lifespan()
universe_path = "/app/data/universe/instrument_universe_v2.json"

def run_callback():
    return run_update_cycle(store, engine, universe_path)

scheduler = UpdateCycleScheduler(
    ScheduleConfig(enabled=True, interval_minutes=1440),
    run_callback,
)
```

All three paths (`astock_data.duckdb`, `universe/instrument_universe_v2.json`, `astock_jobs.db`) follow the existing `DATA_DIR` convention already used in `main.py`, `burnin.py`, and `health.py`.

## 3. Expected execution flow after wiring

```
FastAPI lifespan (main.py)
  ├─ engine = JobEngine(...); engine.start(job_handler)      ← existing
  ├─ scheduler = UpdateCycleScheduler(config, run_callback)  ← new
  └─ background loop (daemon thread or asyncio task):
        every tick (e.g. 60 s):
          result = scheduler.run_once()
          ├─ is_due()?  (in-memory: next_due is None → due immediately;
          │              else now >= next_due)
          │    └─ not due → return None (no-op)
          └─ due → run_callback()
                     → run_update_cycle()          scheduler.py
                        1. coverage snapshot       coverage.py
                        2. update plan             update_planner.py
                        3. materialize jobs        update_executor.py (plan-hash dedup)
                        4. jobs execute            job_engine.py worker → SourceGovernor → DatasetStore
                        5. verify                  run_verifier.py
                        6. lifecycle bookkeeping   run_lifecycle.py / scheduler_state.py
                     → mark_triggered()  (next_due = now + interval_minutes)
  └─ shutdown: stop loop, engine.stop()
```

**Caveats**

- **In-memory trigger state — now durable (P9.4 Step 3)** — the scheduler's `next_due` is in-memory between ticks. Step 3 persists the trigger instant in the JobEngine DB (in a **dedicated `update_cycle_state` table**, separate from `update_runs`) so a restart recovers it and does NOT re-trigger before the interval elapses. The accepted trigger instant is persisted *before* the callback begins, so a crash mid-cycle cannot lose the trigger. `development_state.md` §3 lists the implementation details.
- **Blocking callback** — `run_update_cycle` is synchronous and long-running (coverage + planning + materialization + verification, plus jobs executed asynchronously by the worker). The driver loop must therefore run **off the asyncio event loop** (a worker thread is the natural choice, mirroring JobEngine's single worker thread) or await via `run_in_executor`.
- **Single-flight** — `run_once()` does not guard against re-entry. The driver loop should ensure it is the only caller (serial loop) so a long cycle cannot be re-triggered mid-run.
- **Clean shutdown** — the loop must be stopped (event/flag) in the `finally` block of `lifespan()`, before `engine.stop()`, to avoid triggering a cycle into a half-torn-down engine.

## 4. Minimal implementation plan

**Step 1 — config knob** (`config.py`) ✅ Done

```python
UPDATE_CYCLE_ENABLED   = os.getenv("UPDATE_CYCLE_ENABLED", "false").lower() == "true"
UPDATE_CYCLE_INTERVAL  = int(os.getenv("UPDATE_CYCLE_INTERVAL_MIN", "1440"))
```

Default **off** until wired and regression-tested, consistent with the project's conservative rollout (P9.4 Step 1 shipped the primitive without wiring precisely for this reason).

**Step 2 — driver loop** (new small module, e.g. `update_cycle_service.py`, or inline in `main.py` lifespan) ✅ Done

- Constructor takes `(store, engine, universe_path)`; builds the `run_callback` closure + `UpdateCycleScheduler`.
- `start()` launches a daemon `threading.Thread` running: `while not stop_event.wait(tick_seconds): scheduler.run_once()`.
- `stop()` sets the event and joins with a timeout.
- Ticks are cheap (in-memory comparison) — e.g. 30–60 s cadence independent of `interval_minutes`.

**Step 3 — wire in `main.py` lifespan** ✅ Done

- After `engine.start(job_handler)`: if `UPDATE_CYCLE_ENABLED`, construct and `start()` the service; store on `app.state`.
- In the `finally` block: `service.stop()` **before** `engine.stop()`.

**Step 4 — tests** ✅ Done

- Extend `test_update_cycle_scheduler.py`: a loop test with short interval + pinned clock asserting exactly one callback per interval, and no callback when disabled.
- New `test_update_cycle_wiring.py`: stub `run_update_cycle` (monkeypatched), assert the service loop invokes it once after start, and that shutdown stops it — no real store/engine/DuckDB needed.

**Step 5 — docs** (on completion, per each file's own maintenance note) ✅ Done

- `development_state.md` §1/§3: mark P9.4 Step 2 in-progress → done; remove from pending.
- `project_context.md` §3 row and §4 data-flow diagram: drop "currently unwired".
- `current_architecture.md`: add `update_cycle_scheduler.py` to the module index (R6-7B section or P9.4 row).

**Explicitly out of scope for this step** (matches the project's stated plan):

- Trigger-state persistence (P9.4 Step 3 decision).
- Exposing freshness/coverage as a REST endpoint (limitation #6).
- Changing JobEngine concurrency (limitation #5).

---

*Files inspected: `update_cycle_scheduler.py` (90 lines), `scheduler.py` (168 lines), `tests/test_update_cycle_scheduler.py` (85 lines), `docs/status/{project_context,development_state,current_architecture}.md`, grep of `docker_api/src` + `docker_api/tests`. No other modules scanned; no code modified.*

## Status: Implementation Complete (P9.4 Step 2, uncommitted)

**Implemented 2026-08-26:**

- **Step 1 (config knob):** `UPDATE_CYCLE_ENABLED` (default `"false"`), `UPDATE_CYCLE_INTERVAL_MIN` (1440), `UPDATE_CYCLE_TICK_SECONDS` (60) in `config.py`. ✅
- **Step 2 (driver loop):** `update_cycle_service.py` — `UpdateCycleService` class with daemon-thread driver loop, skip-not-wait single-flight guard (`_in_flight` flag), idempotent `start()`/`stop()`, `stop()` waits for in-flight cycle and reports only after thread join. ✅
- **Step 3 (lifespan wiring):** `main.py` — `_start_update_cycle` constructs `DatasetStore` + `UpdateCycleService` (closure over `run_update_cycle`) and starts it when `UPDATE_CYCLE_ENABLED`; `_stop_update_cycle` calls `service.stop()` **before** `engine.stop()`. ✅
- **Step 4 (tests):** `tests/test_update_cycle_service.py` — 11 tests covering: trigger-once-then-not-before-interval, disabled-never-triggers, stop-halts-loop, start-idempotent, default-callback-invokes-`run_update_cycle`, guard-prevents-overlapping-cycles, skip-instead-of-wait, wiring-starts-when-enabled, wiring-skips-when-disabled, stop-waits-for-in-flight, lifespan-stop-order. All 11 pass. ✅
- **Step 5 (docs):** `development_state.md` §1/§3/§5/§6, `project_context.md` §3/§4/§6, `ARCHITECTURE_REVIEW.md` §2.1/§3/§4/§5, this document. ✅

**Verification:**
- Targeted: `tests/test_update_cycle_service.py` (11) + `tests/test_update_cycle_scheduler.py` (5) = **16/16 pass**.
- Full regression: `pytest tests/` → **680 passed, 2 skipped** (2 skips = Docker-only tests). Up from 617 baseline.
- Signature cross-check: service closure `run_update_cycle(self.store, self.engine, self.universe_path, reference_date=self.reference_date)` matches `run_update_cycle(store, engine, universe_path, reference_date=None, output_dir=None)`. ✅

**P9.4 Step 3 (2026-08-28, uncommitted, corrected) — durable trigger-state persistence:**
- **Durable state:** the trigger instant is persisted in the JobEngine SQLite DB — in a
  **dedicated `update_cycle_state` table** (same DB as the run lifecycle, but a separate
  table so `update_runs` holds only real lifecycle runs and generic readers never surface
  scheduler metadata). No new database, no new subsystem; reuses the existing
  run-lifecycle storage layer.
- **`run_lifecycle.py`:** new `save_trigger_state(engine, iso)` / `load_trigger_state(engine)`
  helpers with a shared `_ensure_trigger_state_table` bootstrap (idempotent `CREATE TABLE IF
  NOT EXISTS update_cycle_state (key TEXT PRIMARY KEY, last_triggered_at TEXT)`).
- **`update_cycle_service.py`:** `UpdateCycleService` loads the persisted instant on
  construction (seeds `last_triggered` + `next_due`) and saves it inside
  `guarded_run_once()` **before** the cycle callback begins (so one trigger → one durable
  save, and a crash mid-cycle cannot lose the persisted trigger). A missing/corrupt/
  read-failed state degrades to first-run semantics; a write failure is logged and never
  breaks the cycle.
- **Semantics:** first run (no state) → due immediately (unchanged); restart before the
  interval → NOT re-triggered; restart after the interval → due again; disabled →
  persistence never causes work; single-flight / skip-not-wait unchanged; production
  `run_update_cycle()` callback wiring unchanged. The plan-hash dedup remains the
  separate, cache-independent idempotency layer.
- **Tests:** `tests/test_update_cycle_trigger_state.py` — 19 tests (first-run,
  due-trigger-advances-once, durable-state-written-before-callback, callback-returning-None,
  callback-raising, reconstruct-before/after-interval, single-flight + skip-not-wait with
  persistence, corrupt/missing state, `update_runs` isolation, lifecycle-readers-only-real-runs,
  stale-run-recovery-ignores-trigger-state, disabled-service, callback-wiring-across-reconstruction,
  save/load round-trip + upsert, write-failure semantics x2). All 19 pass.
- **Verification:** targeted 19/19; regression set (scheduler + service + trigger-state +
  run_lifecycle + scheduler_state + source_state_store + run_verifier + r7_audit) 88/88; full
  `docker_api` regression **699 passed, 2 skipped** (up from 691 at Step 3's first pass).

**P9.4 Step 3 (trigger-state persistence) is complete** — uncommitted, verified and ready
to commit alongside Step 2.
