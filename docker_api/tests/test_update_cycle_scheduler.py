"""Tests for the in-memory scheduled trigger layer.

Only the trigger / time-decision behaviour is exercised here — NOT the update
cycle itself. The callback is a stand-in for ``run_update_cycle()``.
"""

from datetime import datetime, timedelta, timezone

from astock_api.update_cycle_scheduler import (
    ScheduleConfig,
    UpdateCycleScheduler,
)

# A fixed reference instant for deterministic "before / after interval" checks.
T0 = datetime(2026, 8, 24, 0, 0, 0, tzinfo=timezone.utc)


def _counting_callback():
    calls = []

    def cb():
        calls.append(1)
        return "result"

    return cb, calls


def test_new_scheduler_is_due_immediately():
    scheduler = UpdateCycleScheduler(
        ScheduleConfig(enabled=True, interval_minutes=1440), lambda: None
    )
    # A fresh scheduler (never triggered) is due immediately.
    assert scheduler.is_due() is True


def test_not_due_before_interval_after_mark():
    # interval = 1 minute.
    scheduler = UpdateCycleScheduler(
        ScheduleConfig(enabled=True, interval_minutes=1), lambda: None
    )
    scheduler.mark_triggered(now=T0)
    # 30s before the 1-minute interval elapses: not yet due.
    assert scheduler.is_due(now=T0 + timedelta(seconds=30)) is False


def test_due_again_after_interval():
    # interval = 1 minute.
    scheduler = UpdateCycleScheduler(
        ScheduleConfig(enabled=True, interval_minutes=1), lambda: None
    )
    scheduler.mark_triggered(now=T0)
    # Just after the interval elapses: due again.
    assert scheduler.is_due(now=T0 + timedelta(minutes=1)) is True
    # mark_triggered recorded the trigger instant and the next due point.
    assert scheduler.last_triggered == T0
    assert scheduler.next_due == T0 + timedelta(minutes=1)


def test_run_once_calls_callback_exactly_once():
    # Large interval so two near-simultaneous runs cannot both fire.
    cb, calls = _counting_callback()
    scheduler = UpdateCycleScheduler(
        ScheduleConfig(enabled=True, interval_minutes=1440), cb
    )
    r1 = scheduler.run_once()
    assert r1 == "result"
    assert len(calls) == 1
    # Immediately after, the interval has not elapsed -> no second call.
    r2 = scheduler.run_once()
    assert r2 is None
    assert len(calls) == 1


def test_disabled_scheduler_never_runs():
    cb, calls = _counting_callback()
    scheduler = UpdateCycleScheduler(
        ScheduleConfig(enabled=False, interval_minutes=1), cb
    )
    # Even pinning a "past" now, a disabled scheduler is never due.
    assert scheduler.is_due(now=T0) is False
    assert scheduler.is_due() is False
    # And run_once never invokes the callback.
    assert scheduler.run_once(now=T0) is None
    assert scheduler.run_once() is None
    assert len(calls) == 0
