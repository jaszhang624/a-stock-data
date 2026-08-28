"""Update Cycle Service — the driver loop that makes ``UpdateCycleScheduler``
live in the FastAPI process.

This module owns ONLY the *driver*: a single daemon thread that, on a cheap
cadence (``tick_seconds``), calls ``UpdateCycleScheduler.run_once()`` off the
asyncio event loop and, when due, invokes the real production cycle
(``scheduler.run_update_cycle``).

It composes, does NOT modify:
  - ``update_cycle_scheduler.UpdateCycleScheduler``  (time decision + trigger)
  - ``scheduler.run_update_cycle``                   (the cycle itself)

Architecture::

    UpdateCycleService
        |  daemon thread, every tick_seconds
        v
    UpdateCycleScheduler.run_once()
        |  when due
        v
    run_update_cycle(store, engine, universe_path, reference_date)

Design notes
------------
* **Off the event loop** — ``run_update_cycle`` is synchronous and long
  running, so it runs on a worker thread (mirroring ``JobEngine``'s single
  worker thread), never on the asyncio loop.
* **Single-flight, skip-not-wait** — ``guarded_run_once()`` takes the guard
  with ``acquire(blocking=False)``: at most one cycle executes at a time, and
  a concurrent caller returns ``None`` immediately instead of queueing behind
  a long cycle.
* **Single authoritative trigger timestamp** — for a due cycle exactly one
  ``accepted_at = datetime.now(timezone.utc)`` is captured and reused for the
  due check, the durable trigger-state write (which happens *before*
  ``run_callback()`` begins), and ``scheduler.mark_triggered`` (via
  ``run_once(now=accepted_at)``). The persisted ``last_triggered_at`` and
  ``scheduler.last_triggered`` are therefore identical for one accepted
  trigger. A crash mid-cycle cannot lose the trigger.
* **Clean shutdown** — ``stop()`` sets an event and joins the thread with a
  timeout; it returns only once the worker has definitely stopped (or the
  timeout elapsed — then the worker reference is kept and a follow-up
  ``stop()`` retries). ``lifespan`` calls it *before* ``engine.stop()`` so a
  cycle is never triggered into a half-torn-down engine.

Durable trigger state (P9.4 Step 3): the last accepted trigger instant is
loaded on construction (a restarted service is NOT re-triggered before the
interval has elapsed) and re-saved on every accepted trigger — before the
callback begins. The state lives in the dedicated ``update_cycle_state``
table in the JobEngine SQLite DB (same database as the run lifecycle,
separate table — NOT in ``update_runs``); the plan-hash dedup in the cycle
itself remains the cache-independent idempotency layer.

Honest limitation: if the durable write itself fails it is logged, the
in-memory schedule stays advanced for this process, and the cycle continues —
but restart continuity for that trigger cannot be guaranteed (a restart falls
back to first-run semantics). The service makes no exactly-once claim.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from astock_api.config import (
    UPDATE_CYCLE_ENABLED,
    UPDATE_CYCLE_INTERVAL_MIN,
    UPDATE_CYCLE_TICK_SECONDS,
)
from astock_api.run_lifecycle import load_trigger_state, save_trigger_state
from astock_api.update_cycle_scheduler import ScheduleConfig, UpdateCycleScheduler

logger = logging.getLogger(__name__)


class UpdateCycleService:
    """Drive ``UpdateCycleScheduler.run_once()`` from a daemon worker thread.

    Args:
        store: ``DatasetStore`` instance passed to the cycle.
        engine: ``JobEngine`` instance passed to the cycle.
        universe_path: Path to ``instrument_universe_v2.json``.
        config: ``ScheduleConfig``. Defaults to the ``UPDATE_CYCLE_*`` config
            values (enabled flag + interval minutes).
        run_callback: Zero-arg callable invoked when the scheduler is due.
            Defaults to a closure over ``run_update_cycle(store, engine,
            universe_path, reference_date)``. Inject a stub in tests.
        tick_seconds: Seconds between due-checks. Defaults to
            ``UPDATE_CYCLE_TICK_SECONDS``.
        reference_date: Optional YYYY-MM-DD freshness reference passed to the
            cycle each trigger.
    """

    def __init__(
        self,
        store: Any,
        engine: Any,
        universe_path: str,
        config: Optional[ScheduleConfig] = None,
        run_callback: Optional[Callable[[], Any]] = None,
        tick_seconds: Optional[float] = None,
        reference_date: Optional[str] = None,
    ) -> None:
        self.store = store
        self.engine = engine
        self.universe_path = universe_path
        self.reference_date = reference_date
        self.config = config or ScheduleConfig(
            enabled=UPDATE_CYCLE_ENABLED,
            interval_minutes=UPDATE_CYCLE_INTERVAL_MIN,
        )
        self.tick_seconds = (
            tick_seconds
            if tick_seconds is not None
            else float(UPDATE_CYCLE_TICK_SECONDS)
        )
        self.scheduler = UpdateCycleScheduler(
            self.config, run_callback or self._build_default_callback()
        )
        self._load_trigger_state()

        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.RLock()  # serializes start/stop
        self._cycle_lock = threading.Lock()  # single-flight guard
        self._thread: Optional[threading.Thread] = None
        self._started = False

    # ── persistent trigger state (P9.4 Step 3) ─────────────────────────

    def _load_trigger_state(self) -> None:
        """Load the persisted trigger instant into ``self.scheduler``.

        Only active when the engine exposes the JobEngine SQLite DB. A missing
        state row (first run), a corrupt timestamp, or an unreadable DB all
        degrade to the first-run in-memory state (``last_triggered=None`` →
        due on the first tick) rather than crashing the service. The persisted
        value is an ISO-8601 UTC instant; it seeds both ``last_triggered`` and
        ``next_due = last + interval`` so a restarted service does NOT
        re-trigger until the interval has elapsed.
        """
        try:
            ts = load_trigger_state(self.engine)
        except Exception as exc:  # noqa: BLE001 - first-run fallback
            logger.warning(
                "Update cycle trigger-state load failed (%s); using first-run state",
                exc,
            )
            return
        if not ts:
            return  # no persisted state: keep first-run (due immediately)
        try:
            last = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            logger.warning(
                "Update cycle trigger-state value %r is not a valid timestamp; "
                "using first-run state",
                ts,
            )
            return
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        self.scheduler.last_triggered = last
        self.scheduler.next_due = last + timedelta(
            minutes=self.config.interval_minutes
        )
        logger.info(
            "Update cycle trigger state recovered: last_triggered=%s next_due=%s",
            self.scheduler.last_triggered.isoformat(),
            self.scheduler.next_due.isoformat(),
        )

    def _save_trigger_state(self, last_triggered_at: Optional[str] = None) -> None:
        """Persist the accepted trigger instant to the engine's DB.

        Called inside the single-flight guard, BEFORE the cycle callback
        begins (see ``guarded_run_once``): at most one durable save
        accompanies one accepted trigger, and the write precedes the
        long-running callback so a crash mid-cycle cannot lose the trigger.
        When ``last_triggered_at`` is omitted, ``self.scheduler.last_triggered``
        is persisted (for manual/pinned use).

        A write failure is logged but never raised: the in-memory timing
        already advanced for this process and the cycle continues, but
        restart continuity for that trigger cannot be guaranteed — the next
        process boot falls back to first-run semantics rather than failing
        to start. No exactly-once guarantee is claimed.
        """
        value = last_triggered_at
        if value is None and self.scheduler.last_triggered is not None:
            value = self.scheduler.last_triggered.isoformat()
        try:
            save_trigger_state(self.engine, value)
        except Exception as exc:  # noqa: BLE001 - never break the cycle
            logger.error("Update cycle trigger-state save failed: %s", exc)


    # ── callback wiring ────────────────────────────────────────────────

    def _build_default_callback(self) -> Callable[[], Any]:
        """Build the zero-arg closure over the real production cycle.

        ``run_update_cycle`` is imported at call time (not at construction)
        so it can be monkeypatched by tests on the ``astock_api.scheduler``
        module attribute.
        """

        def cb() -> Any:
            from astock_api.scheduler import run_update_cycle

            return run_update_cycle(
                self.store,
                self.engine,
                self.universe_path,
                reference_date=self.reference_date,
            )

        return cb

    # ── single-flight run ──────────────────────────────────────────────

    def guarded_run_once(self) -> Optional[Any]:
        """Call ``scheduler.run_once()`` under the single-flight lock.

        Returns the cycle result when a cycle ran this tick, else ``None``
        (not due / disabled / a cycle is already running). Concurrent callers
        SKIP rather than queue behind an in-flight cycle: the lock is taken
        with ``acquire(blocking=False)``, so a second caller returns ``None``
        immediately instead of waiting for the long cycle to finish.

        Trigger acceptance (P9.4 Step 3): whether a trigger is accepted is
        determined from the scheduler state (``is_due(accepted_at)``), NOT
        from the callback return value — a callback may legitimately return
        ``None``. When a trigger is accepted, exactly one trigger instant
        (``accepted_at = datetime.now(timezone.utc)``) is captured and used
        for the due check, the durable state write (BEFORE the callback
        begins), and ``scheduler.mark_triggered`` via ``run_once(now=...)``,
        so the persisted ``last_triggered_at`` and ``scheduler.last_triggered``
        are identical: at most one save accompanies one trigger, and a crash
        mid-cycle cannot lose the persisted trigger instant. The callback's
        exception propagates unchanged (existing service error semantics);
        the accepted trigger is NOT rolled back.
        """
        if not self._cycle_lock.acquire(blocking=False):
            return None  # a cycle is already running: skip, don't wait
        try:
            # Capture the accepted trigger instant ONCE (P9.4 Step 3). This
            # exact datetime is used for the due check, the durable state
            # write (BEFORE the callback begins), and scheduler.mark_triggered
            # via run_once(now=...), so the persisted last_triggered_at and
            # scheduler.last_triggered are identical for one accepted trigger.
            accepted_at = datetime.now(timezone.utc)
            if self.scheduler.is_due(accepted_at):
                self._save_trigger_state(accepted_at.isoformat())
            return self.scheduler.run_once(now=accepted_at)
        finally:
            self._cycle_lock.release()

    # ── driver loop ────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        logger.info(
            "Update cycle service loop started (tick=%.1fs, interval=%dm, enabled=%s)",
            self.tick_seconds,
            self.config.interval_minutes,
            self.config.enabled,
        )
        # ``stop_event.wait(tick)`` blocks cheaply between checks; it returns
        # ``True`` when stop() was called, so the loop exits without running a
        # final check against a tearing-down engine.
        while not self._stop_event.wait(self.tick_seconds):
            try:
                result = self.guarded_run_once()
                if result is not None:
                    logger.info("Update cycle triggered by service loop")
            except Exception:
                logger.exception("Update cycle service loop error")
        logger.info("Update cycle service loop stopped")

    # ── lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the daemon driver thread. Idempotent.

        After this returns, ``self._thread`` is non-``None`` and
        ``self._started`` is ``True``. A second ``start()`` while the thread
        is alive is a no-op.
        """
        with self._lifecycle_lock:
            if self._started:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop, name="update-cycle-service", daemon=True
            )
            self._thread.start()
            self._started = True

    def _join_worker(self, timeout: float) -> None:
        """Join the worker thread (holding ``_lifecycle_lock``)."""
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _finalize_stop(self) -> None:
        """Clear state (holding ``_lifecycle_lock``); log the outcome."""
        self._thread = None
        self._started = False
        if self._stop_event.is_set():
            logger.info("Update cycle service stopped")

    def stop(self, timeout: float = 15.0) -> None:
        """Signal the driver thread to stop and join it. Idempotent.

        Contract: when this returns normally, the worker thread has
        definitely stopped (or the join timeout elapsed — in which case the
        thread reference is kept, ``started`` is cleared, and a follow-up
        ``stop()`` retries the join). ``stop()`` therefore NEVER reports the
        service stopped while the worker thread is still alive, so the
        lifespan may proceed to ``engine.stop()`` only after it returns.

        Must be called before ``engine.stop()`` in the lifespan teardown.
        """
        with self._lifecycle_lock:
            if not self._started:
                return
            self._stop_event.set()
            self._join_worker(timeout)
            if self._thread is not None and self._thread.is_alive():
                # Worker refused to exit within the timeout (stuck in a
                # callback). Keep the reference so state stays honest:
                # ``running`` reports true and a later ``stop()`` retries.
                logger.error(
                    "Update cycle service stop timed out: worker thread "
                    "still running after %.1fs",
                    timeout,
                )
                self._started = False
                return
            self._finalize_stop()
        self._stop_event.set()  # keep set after the lock for re-entrancy

    @property
    def running(self) -> bool:
        """True while the driver thread is alive."""
        with self._lifecycle_lock:
            return bool(self._thread and self._thread.is_alive())
