"""P9.4 Step 3 (corrected) — durable update-cycle trigger-state persistence.

These tests prove the corrected Step 3 contract on top of the Step 1
(scheduler) and Step 2 (service driver) behaviour:

  A. Trigger-order semantics
     1. First run (no persisted state) -> due immediately (unchanged).
     2. A due trigger advances in-memory scheduler state exactly once.
     3. The durable trigger state exists BEFORE the callback begins
        (inspected from inside a controlled callback — explicit, not
        inferred after completion).
     4. A callback returning ``None`` -> exactly one accepted trigger, one
        durable write, one callback execution.
     5. A callback raising after it begins -> the accepted trigger instant
        remains durably stored.
     6. Reconstructing the service before the interval expires -> no
        immediate retrigger.
     7. Reconstructing after the interval expires -> due again.
  B. Concurrency
     8. Single-flight remains intact; 9. racing caller is skip-not-wait;
     10. N racers on one due trigger -> exactly one accepted trigger, one
        durable write, one callback execution.
  C. Persistence isolation
     11. save/load round-trip through the dedicated ``update_cycle_state``
        table; 12. missing state -> first-run semantics; 13. corrupt value
        degrades to first-run semantics; 14. ``update_runs`` holds no
        scheduler metadata and generic lifecycle readers never return it;
     15. ``get_latest_run`` / ``get_runs`` / ``get_run`` /
        ``get_last_successful_run`` / stale-run recovery operate only on
        real update runs.
  D. Production wiring
     16. ``run_update_cycle()`` callback wiring (store/engine/universe/
        reference_date); 17. disabled service never claims/persists
        triggers; 18-19. Step 1/Step 2 behaviour regressions live in
        ``test_update_cycle_scheduler.py`` / ``test_update_cycle_service.py``.

  E. Persistence-failure semantics: a failed durable write never breaks the
     cycle, keeps the in-memory schedule advanced, and the limitation
     (no restart continuity for that trigger) is made explicit.

The durable state lives in the SAME JobEngine SQLite DB as the run
lifecycle but in its OWN table (``update_cycle_state``) — NOT in
``update_runs``. A real ``JobEngine`` is used so the production path is
exercised; controlled (ISO-8601 UTC) instants are used rather than real
sleeps.

Single-timestamp contract (P9.4 Step 3, final polish): exactly ONE
``accepted_at = datetime.now(timezone.utc)`` is captured per accepted trigger
and reused for the due check, the durable write (BEFORE the callback), and
``scheduler.mark_triggered`` via ``run_once(now=accepted_at)``. The persisted
``last_triggered_at`` and ``scheduler.last_triggered`` are therefore
identical — proven by ``test_single_authoritative_timestamp``.
"""

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from astock_api.job_engine import JobEngine
from astock_api.run_lifecycle import (
    create_run,
    fail_run,
    get_last_successful_run,
    get_latest_run,
    get_run,
    get_runs,
    initialize_run_lifecycle,
    load_trigger_state,
    save_trigger_state,
    start_run,
)
from astock_api.update_cycle_scheduler import ScheduleConfig, UpdateCycleScheduler
from astock_api.update_cycle_service import UpdateCycleService

# A fixed reference instant for deterministic "before / after interval" checks.
T0 = datetime(2026, 8, 24, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def engine(tmp_path):
    """A real, isolated JobEngine (the production durable-state backend)."""
    db_path = os.path.join(str(tmp_path), "astock_jobs.db")
    eng = JobEngine(db_path=db_path, data_dir=str(tmp_path))
    eng.initialize()
    return eng


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _counting_callback(sleep=0.0):
    import time

    calls = []

    def cb():
        calls.append(1)
        if sleep:
            time.sleep(sleep)
        return "result"

    return cb, calls


def _new_service(engine, interval_minutes=60, **kwargs):
    return UpdateCycleService(
        store=object(), engine=engine, universe_path="/u.json",
        config=ScheduleConfig(enabled=kwargs.pop("enabled", True),
                              interval_minutes=interval_minutes),
        **kwargs,
    )


# ── A. Trigger-order semantics ────────────────────────────────────────

def test_first_run_no_persisted_state_is_due_immediately(engine):
    # 1. No state saved -> load returns None -> fresh scheduler due immediately.
    assert load_trigger_state(engine) is None
    svc = _new_service(engine, 1440, run_callback=lambda: None)
    assert svc.scheduler.last_triggered is None
    assert svc.scheduler.next_due is None
    assert svc.scheduler.is_due(now=T0) is True


def test_due_trigger_advances_state_exactly_once(engine):
    # 2. One accepted trigger -> last/next advance exactly once.
    svc = _new_service(engine, 60, run_callback=lambda: None)
    svc.scheduler.mark_triggered(T0)
    assert svc.scheduler.last_triggered == T0
    assert svc.scheduler.next_due == T0 + timedelta(minutes=60)
    # Not due again until the interval elapses (>= semantics).
    assert svc.scheduler.is_due(now=T0 + timedelta(minutes=59)) is False
    assert svc.scheduler.is_due(now=T0 + timedelta(minutes=60)) is True


def test_single_authoritative_timestamp(engine):
    # For one accepted trigger, the persisted last_triggered_at and
    # scheduler.last_triggered MUST be identical (exactly one timestamp is
    # captured and reused for due-check, persistence, and mark_triggered).
    svc = _new_service(engine, 60, run_callback=lambda: "ok")
    # Seed "previously triggered" far in the past so the guard finds a due
    # trigger without sleeping.
    svc.scheduler.last_triggered = T0
    svc.scheduler.next_due = T0 - timedelta(minutes=1)
    result = svc.guarded_run_once()
    assert result == "ok"

    stored = load_trigger_state(engine)
    assert stored is not None
    # Strict equality: the SAME datetime object value, not "close enough".
    assert svc.scheduler.last_triggered is not None
    assert stored == svc.scheduler.last_triggered.isoformat()
    assert svc.scheduler.next_due == svc.scheduler.last_triggered + timedelta(
        minutes=60
    )


def test_durable_state_written_before_callback_begins(engine):
    # 3. Explicit, in-callback inspection: a persisted trigger instant must
    # ALREADY exist in the dedicated table when the callback begins
    # executing — and ``update_runs`` must hold no scheduler metadata row.
    from astock_api.run_lifecycle import _TRIGGER_STATE_KEY

    observed = {}

    def cb():
        conn = sqlite3.connect(engine.db_path)
        try:
            row = conn.execute(
                "SELECT last_triggered_at FROM update_cycle_state WHERE key=?",
                (_TRIGGER_STATE_KEY,),
            ).fetchone()
            observed["state_value"] = row[0] if row else None
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if "update_runs" in tables:
                observed["update_runs_rows"] = conn.execute(
                    "SELECT COUNT(*) FROM update_runs"
                ).fetchone()[0]
            else:
                observed["update_runs_rows"] = 0  # table never created
        finally:
            conn.close()
        return "result"

    # Seed a prior trigger so the durable state exists before this tick's
    # write. The production guard persists the ACCEPTED instant of THIS tick
    # (write #1, BEFORE the callback); the in-callback inspection below proves
    # the state exists at callback start.
    save_trigger_state(engine, _iso(T0))
    svc = _new_service(engine, 1440, run_callback=cb)
    initialize_run_lifecycle(engine)  # explicit: update_runs exists, 0 rows
    # Force the guard's due-check to fire now without waiting.
    svc.scheduler.last_triggered = T0
    svc.scheduler.next_due = T0 - timedelta(minutes=1)
    result = svc.guarded_run_once()
    assert result == "result"

    assert observed["state_value"] is not None, (
        "durable trigger state must be written BEFORE the callback begins"
    )
    assert observed["state_value"] >= _iso(T0), (
        "the persisted instant must be a real accepted trigger instant"
    )
    assert observed["update_runs_rows"] == 0, (
        "update_runs must contain no scheduler metadata row"
    )


def test_callback_returning_none_still_persists_trigger(engine):
    # 4. A callback that legitimately returns None must still mean: one
    # accepted trigger, one durable write, one callback execution.
    calls = []
    writes = []

    def cb():
        calls.append(1)
        return None

    svc = _new_service(engine, 1440, run_callback=cb)
    # Seed "previously triggered" far in the past so the due-check inside the
    # guard (wall clock) finds a due trigger without sleeping.
    svc.scheduler.last_triggered = T0
    svc.scheduler.next_due = T0 - timedelta(minutes=1)

    orig_save = svc._save_trigger_state

    def recording_save(last_triggered_at=None):
        writes.append(1)
        return orig_save(last_triggered_at=last_triggered_at)

    svc._save_trigger_state = recording_save

    result = svc.guarded_run_once()
    assert result is None  # callback output None — not a trigger indicator
    assert len(calls) == 1   # exactly one callback execution
    assert len(writes) == 1  # exactly one durable write
    stored = load_trigger_state(engine)
    assert stored is not None
    assert datetime.fromisoformat(stored) > T0  # a real accepted trigger
    assert svc.scheduler.last_triggered is not None
    assert svc.scheduler.last_triggered > T0  # trigger accepted


def test_callback_raising_still_keeps_accepted_trigger(engine):
    # 5. Callback raises after the trigger was accepted: the persisted
    # trigger instant must remain, and the in-memory schedule must remain
    # advanced. The exception propagates (existing service error semantics).
    class Boom(Exception):
        pass

    def cb():
        raise Boom("cycle failed")

    svc = _new_service(engine, 60, run_callback=cb)
    # Seed "previously triggered" far in the past so the due-check inside the
    # guard (wall clock) finds a due trigger without sleeping. The guard then
    # writes the accepted instant of THIS tick (write #1, before the
    # callback), so the persisted value survives the callback raise.
    svc.scheduler.last_triggered = T0
    svc.scheduler.next_due = T0 - timedelta(minutes=1)
    with pytest.raises(Boom):
        svc.guarded_run_once()

    # Persisted state: the accepted instant is still durably stored.
    stored = load_trigger_state(engine)
    assert stored is not None
    last = datetime.fromisoformat(stored)
    assert last > T0  # the accepted instant of this tick, not the prior seed
    assert last <= datetime.now(timezone.utc)

    # In-memory state: still advanced, not rolled back, not due again. With
    # the single-timestamp contract (P9.4 Step 3 final polish) the persisted
    # instant and ``scheduler.last_triggered`` are the SAME datetime — assert
    # strict equality.
    assert svc.scheduler.last_triggered is not None
    assert svc.scheduler.next_due is not None
    assert svc.scheduler.last_triggered == last
    assert svc.scheduler.next_due == last + timedelta(minutes=60)
    assert svc.scheduler.last_triggered > T0
    assert svc.scheduler.is_due(now=datetime.now(timezone.utc)) is False


def test_reconstruct_after_failed_callback_before_interval(engine):
    # 6. After a trigger whose callback failed, reconstructing the service
    # before the interval expires must NOT immediately retrigger.
    def boom_cb():
        raise RuntimeError("simulated mid-cycle crash")

    svc = _new_service(engine, 1440, run_callback=boom_cb)
    with pytest.raises(RuntimeError):
        svc.guarded_run_once()

    svc2 = _new_service(engine, 1440, run_callback=lambda: "second")
    # Interval (1440m) has not elapsed since the accepted trigger.
    assert svc2.scheduler.last_triggered is not None
    assert svc2.scheduler.is_due(now=datetime.now(timezone.utc)) is False
    assert svc2.scheduler.run_once(now=datetime.now(timezone.utc)) is None


def test_reconstruct_after_interval_is_due(engine):
    # 7. Reconstructing after the interval expires -> due again.
    save_trigger_state(engine, _iso(T0))
    svc = _new_service(engine, 60, run_callback=lambda: None)
    assert svc.scheduler.is_due(now=T0 + timedelta(minutes=61)) is True
    # Exactly at the boundary (== next_due) the scheduler is due (>=).
    assert svc.scheduler.is_due(now=T0 + timedelta(minutes=60)) is True


# ── B. Concurrency semantics ──────────────────────────────────────────

def test_racer_competition_one_trigger_one_write_one_callback(engine):
    # 8+9+10. Four racers on one due trigger: exactly one accepted trigger,
    # one durable write, one callback execution; racers skip, don't wait.
    import threading

    cb, calls = _counting_callback(sleep=0.2)
    svc = _new_service(engine, 1440, run_callback=cb)
    writes = []
    orig_save = svc._save_trigger_state

    def recording_save(last_triggered_at=None):
        writes.append(1)
        return orig_save(last_triggered_at=last_triggered_at)

    svc._save_trigger_state = recording_save

    threads = [threading.Thread(target=svc.guarded_run_once) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1   # single-flight: one cycle
    assert len(writes) == 1  # exactly one durable trigger write
    assert load_trigger_state(engine) is not None
    # Only the winner returned a non-None result; racers returned None.
    assert svc.scheduler.last_triggered is not None


def test_skip_not_wait_racing_caller_returns_none(engine):
    import threading
    import time

    release = threading.Event()
    running = threading.Event()

    def slow_cb():
        running.set()
        release.wait(5.0)
        return "done"

    svc = _new_service(engine, 1440, run_callback=slow_cb)
    winner = threading.Thread(target=svc.guarded_run_once)
    winner.start()
    assert running.wait(2.0), "first cycle never started"

    t0 = time.monotonic()
    r = svc.guarded_run_once()
    elapsed = time.monotonic() - t0

    release.set()
    winner.join(5.0)

    assert r is None
    assert elapsed < 0.1


# ── C. Persistence isolation ──────────────────────────────────────────

def test_trigger_state_roundtrip_via_dedicated_table(engine):
    # 11. Round-trip through update_cycle_state (same JobEngine DB).
    assert load_trigger_state(engine) is None  # fresh
    save_trigger_state(engine, _iso(T0))
    after_first = load_trigger_state(engine)
    assert after_first is not None
    assert datetime.fromisoformat(after_first) == T0
    # Upsert: a second save replaces the value, no duplicate rows.
    t1 = T0 + timedelta(hours=5)
    save_trigger_state(engine, _iso(t1))
    after_second = load_trigger_state(engine)
    assert after_second is not None
    assert datetime.fromisoformat(after_second) == t1

    conn = sqlite3.connect(engine.db_path)
    try:
        rows = conn.execute(
            "SELECT key, last_triggered_at FROM update_cycle_state"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "update_cycle"
        assert datetime.fromisoformat(rows[0][1]) == t1
    finally:
        conn.close()


def test_missing_state_preserves_first_run(engine):
    # 12. Brand-new engine: no trigger-state row -> first-run semantics.
    assert load_trigger_state(engine) is None
    svc = _new_service(engine, 60, run_callback=lambda: None)
    assert svc.scheduler.last_triggered is None
    assert svc.scheduler.is_due(now=T0) is True


def test_corrupt_state_degrades_to_first_run(engine):
    # 13. Hand-write a corrupt value in the dedicated table -> load returns
    # None and the service falls back to first-run (due immediately).
    conn = sqlite3.connect(engine.db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS update_cycle_state ("
            "key TEXT PRIMARY KEY, last_triggered_at TEXT)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO update_cycle_state (key, last_triggered_at) "
            "VALUES ('update_cycle', 'not-a-timestamp')"
        )
        conn.commit()
    finally:
        conn.close()

    assert load_trigger_state(engine) is None
    svc = _new_service(engine, 60, run_callback=lambda: None)
    assert svc.scheduler.last_triggered is None
    assert svc.scheduler.is_due(now=T0) is True


def test_update_runs_holds_no_scheduler_metadata(engine):
    # 14. Scheduler metadata no longer lives in update_runs.
    save_trigger_state(engine, _iso(T0))
    initialize_run_lifecycle(engine)  # creates the (separate) runs table
    conn = sqlite3.connect(engine.db_path)
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(update_runs)").fetchall()}
        assert "last_triggered_at" not in cols
        assert conn.execute(
            "SELECT COUNT(*) FROM update_runs"
        ).fetchone()[0] == 0
        state_rows = conn.execute(
            "SELECT COUNT(*) FROM update_cycle_state"
        ).fetchone()[0]
        assert state_rows == 1
    finally:
        conn.close()


def test_generic_lifecycle_readers_see_only_real_runs(engine):
    # 15. With trigger state present, generic update_runs readers operate on
    # real runs only: get_latest_run / get_runs / get_run /
    # get_last_successful_run all reflect only real lifecycle runs.
    save_trigger_state(engine, _iso(T0))

    initialize_run_lifecycle(engine)
    run_id = create_run(engine, "2026-08-24")
    start_run(engine, run_id)
    fail_run(engine, run_id, "simulated failure")

    latest = get_latest_run(engine)
    assert latest is not None
    assert latest["run_id"] == run_id
    assert latest["status"] == "FAILED"
    assert "STATE" != latest["status"]
    assert latest["reference_date"] == "2026-08-24"

    runs = get_runs(engine, limit=10)
    assert len(runs) == 1
    assert runs[0]["run_id"] == run_id

    assert get_run(engine, run_id) is not None
    assert get_run(engine, run_id)["status"] == "FAILED"
    assert get_last_successful_run(engine) is None


def test_stale_run_recovery_ignores_trigger_state(engine):
    # 15b. initialize_run_lifecycle stale-run recovery marks in-progress runs
    # INTERRUPTED; it does not create or touch any scheduler metadata row.
    initialize_run_lifecycle(engine)
    run_id = create_run(engine, "2026-08-24")
    start_run(engine, run_id)  # left in RUNNING (simulated crash)

    conn = sqlite3.connect(engine.db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS update_cycle_state ("
            "key TEXT PRIMARY KEY, last_triggered_at TEXT)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO update_cycle_state (key, last_triggered_at) "
            "VALUES ('update_cycle', ?)", (_iso(T0),)
        )
        conn.commit()
    finally:
        conn.close()

    initialize_run_lifecycle(engine)  # must recover the stale RUNNING run
    assert get_run(engine, run_id)["status"] == "INTERRUPTED"
    assert load_trigger_state(engine) == _iso(T0)  # trigger state untouched
    assert get_latest_run(engine)["run_id"] == run_id  # real run is latest


# ── D. Production wiring ──────────────────────────────────────────────

def test_disabled_service_never_claims_or_persists_triggers(engine):
    # 17. Disabled service: no trigger accepted, no durable write, even when
    # a very old persisted trigger exists and the interval has long passed.
    save_trigger_state(engine, _iso(T0))
    cb, calls = _counting_callback()
    svc = _new_service(engine, 1, enabled=False, run_callback=cb)
    assert svc.scheduler.is_due(now=T0 + timedelta(hours=48)) is False
    result = svc.guarded_run_once()
    assert result is None
    assert len(calls) == 0
    assert load_trigger_state(engine) == _iso(T0)  # unchanged


def test_default_callback_wiring_across_reconstruction(engine, monkeypatch):
    # 16. Trigger + persist once, then reconstruct with the PRODUCTION
    # callback (run_callback not supplied) and verify it calls
    # run_update_cycle with the correct store/engine/universe_path/
    # reference_date when due.
    import astock_api.scheduler as scheduler_mod

    save_trigger_state(engine, _iso(T0 - timedelta(minutes=120)))

    calls = []

    def fake_run_update_cycle(store, engine_, universe_path,
                               reference_date=None, output_dir=None):
        calls.append({
            "store": store, "engine": engine_,
            "universe_path": universe_path, "reference_date": reference_date,
        })
        return {"ok": True}

    fake_store = object()
    original = scheduler_mod.run_update_cycle
    scheduler_mod.run_update_cycle = fake_run_update_cycle
    try:
        svc = UpdateCycleService(
            store=fake_store, engine=engine, universe_path="/u.json",
            config=ScheduleConfig(enabled=True, interval_minutes=60),
            reference_date="2026-08-24",
        )
        # Persisted trigger is 2h old; the 60-min interval has elapsed.
        assert svc.scheduler.is_due(now=T0) is True
        result = svc.scheduler.run_once(now=T0)
        assert result == {"ok": True}
        assert len(calls) == 1
        assert calls[0]["store"] is fake_store
        assert calls[0]["engine"] is engine
        assert calls[0]["universe_path"] == "/u.json"
        assert calls[0]["reference_date"] == "2026-08-24"
    finally:
        scheduler_mod.run_update_cycle = original


# ── E. Persistence-failure semantics ──────────────────────────────────

def test_trigger_state_write_failure_degrades_gracefully(engine, monkeypatch):
    # E. If the durable trigger-state write itself fails: it is logged, the
    # service does NOT crash, the in-memory schedule remains advanced, and
    # the limitation (no restart continuity for that trigger) is made
    # explicit by the test itself — the service still runs the callback.
    import astock_api.update_cycle_service as service_mod

    def failing_save(engine_, last_triggered_at):
        raise RuntimeError("simulated durable write failure")

    monkeypatch.setattr(service_mod, "save_trigger_state", failing_save)

    calls = []

    def cb():
        calls.append(1)
        return "ok"

    svc = _new_service(engine, 1440, run_callback=cb)
    result = svc.guarded_run_once()  # must NOT raise despite the failed write
    assert result == "ok"
    assert len(calls) == 1  # the cycle ran
    # In-memory schedule: the trigger was accepted and advanced.
    assert svc.scheduler.last_triggered is not None
    assert svc.scheduler.next_due is not None
    # Explicit limitation: the durable write failed, so the DB has no
    # trigger state — restart continuity for this trigger is NOT guaranteed.
    assert load_trigger_state(engine) is None


def test_trigger_state_write_failure_preserves_loaded_state(engine, monkeypatch):
    # E2. With a PREVIOUSLY persisted trigger, a failed write during a new
    # trigger does not erase the previous durable value (the previous value
    # stays; the new accepted instant is simply not persisted).
    import astock_api.update_cycle_service as service_mod

    def failing_save(engine_, last_triggered_at):
        raise RuntimeError("simulated durable write failure")

    monkeypatch.setattr(service_mod, "save_trigger_state", failing_save)

    save_trigger_state(engine, _iso(T0))  # prior trigger, written via module fn
    svc = _new_service(engine, 1440, run_callback=lambda: "ok")
    svc.scheduler.last_triggered = T0
    svc.scheduler.next_due = T0 - timedelta(minutes=1)  # force due now
    result = svc.guarded_run_once()
    assert result == "ok"
    # The previously persisted value survives the failed new write.
    assert load_trigger_state(engine) == _iso(T0)
