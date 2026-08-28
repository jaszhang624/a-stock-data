"""Tests for the UpdateCycleService production driver loop.

These exercise the wiring layer (UpdateCycleService), not the update cycle
itself: the callback is a stand-in unless a test explicitly monkeypatches
``astock_api.scheduler.run_update_cycle``. No real store/engine/DuckDB is
required.

Tests also cover:
  - single-flight skip-not-wait (a racing ``guarded_run_once()`` returns
    immediately with ``None`` while a cycle is in flight),
  - ``stop()`` while a cycle is actively running (returns only after the
    worker has actually stopped; idempotent),
  - the real lifespan teardown path (``main._stop_update_cycle``) with a
    real service mid-cycle, asserting service-stop before engine-stop.

The wiring tests target the main.py lifespan branching through the
``_start_update_cycle`` helper — the exact function the lifespan calls —
so they verify the real wiring path (enabled -> constructed + started +
registered on app.state; disabled -> nothing is constructed).
"""

import threading
import time
from types import SimpleNamespace

from astock_api.update_cycle_scheduler import ScheduleConfig
from astock_api.update_cycle_service import UpdateCycleService


def _counting_callback(sleep=0.0):
    calls = []

    def cb():
        calls.append(1)
        if sleep:
            time.sleep(sleep)
        return "result"

    return cb, calls


def _wait_until(predicate, timeout=2.0, interval=0.02):
    """Poll ``predicate()`` until true or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_service_triggers_callback_once_then_not_before_interval():
    # Large interval: once triggered, it will not be due again within this test.
    cb, calls = _counting_callback()
    service = UpdateCycleService(
        store=object(), engine=object(), universe_path="/u.json",
        config=ScheduleConfig(enabled=True, interval_minutes=1440),
        run_callback=cb, tick_seconds=0.05,
    )
    service.start()
    try:
        # Exactly one trigger expected (a fresh scheduler is due immediately).
        assert _wait_until(lambda: len(calls) >= 1)
        # Give the loop a few more ticks: the interval has not elapsed, so no
        # second run.
        time.sleep(0.2)
    finally:
        service.stop()
    assert len(calls) == 1


def test_disabled_service_never_triggers():
    cb, calls = _counting_callback()
    service = UpdateCycleService(
        store=object(), engine=object(), universe_path="/u.json",
        config=ScheduleConfig(enabled=False, interval_minutes=1),
        run_callback=cb, tick_seconds=0.05,
    )
    service.start()
    try:
        time.sleep(0.2)
    finally:
        service.stop()
    assert len(calls) == 0


def test_stop_halts_the_loop_and_prevents_further_triggers():
    cb, calls = _counting_callback()
    service = UpdateCycleService(
        store=object(), engine=object(), universe_path="/u.json",
        config=ScheduleConfig(enabled=True, interval_minutes=1440),
        run_callback=cb, tick_seconds=0.05,
    )
    service.start()
    try:
        assert _wait_until(lambda: len(calls) >= 1)
    finally:
        service.stop()
    # After stop the worker thread must be gone and no further ticks may run.
    assert service._started is False
    assert service._thread is None
    before = len(calls)
    time.sleep(0.15)
    assert len(calls) == before


def test_start_is_idempotent():
    cb, calls = _counting_callback()
    service = UpdateCycleService(
        store=object(), engine=object(), universe_path="/u.json",
        config=ScheduleConfig(enabled=True, interval_minutes=1440),
        run_callback=cb, tick_seconds=0.05,
    )
    service.start()
    try:
        first_thread = service._thread
        service.start()  # idempotent: must not relaunch the worker
        assert service._thread is first_thread  # same thread object reused
    finally:
        service.stop()


def test_default_callback_invokes_run_update_cycle():
    # The production path: run_callback not supplied -> closure over
    # astock_api.scheduler.run_update_cycle.
    import astock_api.scheduler as scheduler_mod

    calls = []

    def fake_run_update_cycle(store, engine, universe_path,
                               reference_date=None, output_dir=None):
        calls.append({
            "store": store, "engine": engine,
            "universe_path": universe_path, "reference_date": reference_date,
        })
        return {"ok": True}

    fake_store, fake_engine = object(), object()
    service = UpdateCycleService(
        store=fake_store, engine=fake_engine,
        universe_path="/u.json",
        config=ScheduleConfig(enabled=True, interval_minutes=1440),
        reference_date="2026-08-24", tick_seconds=0.05,
    )
    original = scheduler_mod.run_update_cycle
    scheduler_mod.run_update_cycle = fake_run_update_cycle
    service.start()
    try:
        assert _wait_until(lambda: len(calls) >= 1)
    finally:
        service.stop()
        scheduler_mod.run_update_cycle = original

    assert len(calls) == 1
    assert calls[0]["store"] is fake_store
    assert calls[0]["engine"] is fake_engine
    assert calls[0]["universe_path"] == "/u.json"
    assert calls[0]["reference_date"] == "2026-08-24"


def test_guard_prevents_overlapping_cycles():
    # A slow callback: many threads racing on guarded_run_once must yield
    # exactly one cycle (single-flight guard).
    cb, calls = _counting_callback(sleep=0.2)
    service = UpdateCycleService(
        store=object(), engine=object(), universe_path="/u.json",
        config=ScheduleConfig(enabled=True, interval_minutes=1440),
        run_callback=cb,
    )
    results = []
    results_lock = threading.Lock()

    def worker():
        r = service.guarded_run_once()
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1  # only one cycle executed despite 4 racers
    assert sum(1 for r in results if r is not None) == 1  # only one winner


def test_guarded_run_once_skips_instead_of_waiting():
    # Single-flight is skip-not-wait: while the first cycle is still running,
    # a concurrent guarded_run_once() must return immediately with None,
    # NOT block until the long callback finishes.
    release = threading.Event()
    running = threading.Event()
    calls = []

    def slow_cb():
        calls.append(1)
        running.set()
        release.wait(5.0)
        return "done"

    service = UpdateCycleService(
        store=object(), engine=object(), universe_path="/u.json",
        config=ScheduleConfig(enabled=True, interval_minutes=1440),
        run_callback=slow_cb,
    )
    winner_thread = threading.Thread(target=service.guarded_run_once)
    winner_thread.start()
    assert running.wait(2.0), "first cycle never started"

    # The long cycle is in flight; the racing caller must skip.
    t0 = time.monotonic()
    r = service.guarded_run_once()  # running in the main thread
    elapsed = time.monotonic() - t0

    release.set()
    winner_thread.join(5.0)

    assert r is None  # skipped: no second cycle result
    assert elapsed < 0.1  # returned without waiting for the long callback


# ── main.py lifespan branching (targeted wiring) ─────────────────────
class _RecordingService:
    """Fake UpdateCycleService: records construction and start/stop calls."""

    instances = []

    def __init__(self, **kwargs):
        self.args = kwargs
        self.started = False
        self.stopped = False
        _RecordingService.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def _fresh_recording():
    _RecordingService.instances.clear()
    return _RecordingService


def test_wiring_starts_and_registers_service_when_enabled(monkeypatch):
    import astock_api.main as main_mod
    import astock_api.config as config

    RecordingService = _fresh_recording()
    monkeypatch.setattr(config, "UPDATE_CYCLE_ENABLED", True)
    monkeypatch.setattr(main_mod, "UpdateCycleService", RecordingService)

    app = SimpleNamespace(state=SimpleNamespace())
    engine, store, universe = object(), object(), "/u.json"
    svc = main_mod._start_update_cycle(app, engine, store, universe)

    assert svc is not None
    assert svc.started is True  # the service was started
    assert app.state.update_cycle_service is svc  # registered on app.state
    assert svc.args["store"] is store
    assert svc.args["engine"] is engine
    assert svc.args["universe_path"] == universe
    assert len(RecordingService.instances) == 1  # constructed exactly once


def test_wiring_skips_service_when_disabled(monkeypatch):
    import astock_api.main as main_mod
    import astock_api.config as config

    def _explode(**kwargs):
        raise AssertionError("service must not be constructed when disabled")

    monkeypatch.setattr(config, "UPDATE_CYCLE_ENABLED", False)
    monkeypatch.setattr(main_mod, "UpdateCycleService", _explode)

    app = SimpleNamespace(state=SimpleNamespace())
    svc = main_mod._start_update_cycle(app, object(), object(), "/u.json")

    assert svc is None
    assert not hasattr(app.state, "update_cycle_service")


# ── lifecycle: shutdown while a cycle is actively running ────────────

def test_stop_waits_for_in_flight_cycle_before_reporting_stopped():
    # Stop semantics: stop() must never report the service stopped while the
    # worker thread is still alive — even when a cycle is actively running.
    release = threading.Event()
    cycle_active = threading.Event()
    calls = []

    def slow_cb():
        calls.append(1)
        cycle_active.set()
        release.wait(5.0)
        return "done"

    service = UpdateCycleService(
        store=object(), engine=object(), universe_path="/u.json",
        config=ScheduleConfig(enabled=True, interval_minutes=1440),
        run_callback=slow_cb, tick_seconds=0.05,
    )
    service.start()
    try:
        assert _wait_until(lambda: cycle_active.is_set()), "cycle never started"
        # Now shut down while the cycle is still inside the callback.
        t0 = time.monotonic()
        service.stop(timeout=5.0)
        stop_elapsed = time.monotonic() - t0
    finally:
        release.set()
        service.stop()  # idempotent: must not raise or hang

    # While stop() was returning, the worker must already be gone.
    assert service.running is False
    assert service._thread is None
    assert service._started is False
    # And stop() waited for the in-flight cycle to finish (didn't abandon it).
    assert cycle_active.is_set() and len(calls) == 1
    assert stop_elapsed >= 0.05  # it actually waited, not a no-op early return

    # Re-stop after everything is quiescent: still clean.
    service.stop()
    assert service.running is False


# ── real lifespan lifecycle: teardown ordering ───────────────────────

def test_lifespan_stops_cycle_service_before_job_engine():
    # Real wiring path: the exact main.lifespan teardown sequence must stop
    # the update-cycle service BEFORE the job engine. The service is a REAL
    # UpdateCycleService (wrapper records only the stop order); the engine
    # records its own shutdown.
    import astock_api.main as main_mod
    from types import SimpleNamespace

    shutdown_order = []

    class RealServiceWithOrder:
        """Delegates to a real UpdateCycleService; records stop() position."""

        def __init__(self, real):
            self.real = real

        def stop(self, *args, **kwargs):
            shutdown_order.append("service_stop")
            self.real.stop(*args, **kwargs)

    class EngineRecorder:
        def stop(self):
            shutdown_order.append("engine_stop")

    service = RealServiceWithOrder(UpdateCycleService(
        store=object(), engine=object(), universe_path="/u.json",
        config=ScheduleConfig(enabled=True, interval_minutes=1440),
        run_callback=lambda: "noop", tick_seconds=0.05,
    ))
    service.real.start()
    app = SimpleNamespace(state=SimpleNamespace())
    try:
        # Drive the exact lifespan teardown path while NO cycle is in flight.
        main_mod._stop_update_cycle(app, service, EngineRecorder())
        assert shutdown_order == ["service_stop", "engine_stop"]
        assert service.real.running is False  # worker actually stopped
        assert app.state.update_cycle_service is None  # cleared on app.state
    finally:
        service.real.stop()  # idempotent cleanup
