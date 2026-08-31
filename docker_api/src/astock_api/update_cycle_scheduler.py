"""Update Cycle Scheduler — a minimal, in-memory scheduled trigger layer.

This module owns ONLY the time decision and the trigger call. It is NOT the
cycle executor: the existing ``scheduler.run_update_cycle()`` still owns
coverage, planning, materialization, verification, and lifecycle.

Architecture::

    UpdateCycleScheduler
        |
        | when due
        v
    run_update_cycle()      (injected as ``run_callback``)

**Ordering guarantee (P9.4 Step 3):** when a cycle is due, the trigger instant is
accepted, ``mark_triggered()`` advances the in-memory state, and the callback
runs — in that order. Trigger acceptance therefore happens *before* the
potentially long-running callback, so a crash mid-callback cannot lose the
trigger from the in-memory schedule. Durable persistence of the accepted instant
belongs to the driver layer (``UpdateCycleService``), which writes it to the
JobEngine DB *before* the callback begins.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional


def _utcnow() -> datetime:
    """Current time in UTC (single source so tests can pin ``now``)."""
    return datetime.now(timezone.utc)


@dataclass
class ScheduleConfig:
    """Configuration for the update cycle scheduler.

    Attributes:
        enabled: When ``False`` the scheduler never triggers.
        interval_minutes: Minutes between cycle triggers. Default 1440 (24h).
    """

    enabled: bool = True
    interval_minutes: int = 1440


class UpdateCycleScheduler:
    """Time-based trigger for the automated update cycle.

    Holds only in-memory state (``last_triggered`` / ``next_due``). A fresh
    scheduler is due immediately on its first tick; after ``mark_triggered()``
    it is not due again until ``interval_minutes`` have elapsed.
    """

    def __init__(self, config: ScheduleConfig, run_callback: Callable[[], Any]):
        self.config = config
        self.run_callback = run_callback
        self.last_triggered: Optional[datetime] = None
        # ``None`` means "never triggered" -> due immediately on first tick.
        self.next_due: Optional[datetime] = None

    def is_due(self, now: Optional[datetime] = None) -> bool:
        """Return ``True`` when a cycle should run."""
        if not self.config.enabled:
            return False
        if now is None:
            now = _utcnow()
        if self.next_due is None:
            return True
        return now >= self.next_due

    def mark_triggered(self, now: Optional[datetime] = None) -> None:
        """Record a trigger at ``now``: set ``last_triggered`` and advance
        ``next_due`` by ``interval_minutes``."""
        if now is None:
            now = _utcnow()
        self.last_triggered = now
        self.next_due = now + timedelta(minutes=self.config.interval_minutes)

    def run_once(self, now: Optional[datetime] = None) -> Optional[Any]:
        """If due: accept one trigger, advance state, then call ``run_callback()``.

        Sequence for a due cycle: the trigger instant ``now`` is accepted,
        ``mark_triggered(now)`` advances ``last_triggered`` / ``next_due``, and
        only then is ``run_callback()`` invoked. If the callback raises, the
        advanced in-memory state is retained (a failed trigger is still a
        consumed trigger); the exception propagates to the caller.

        Returns the callback's execution result when a trigger ran, or
        ``None`` when the scheduler was not due (or disabled). The return
        value is the *callback output* — it is NOT a trigger indicator (a
        callback may legitimately return ``None``); callers that need to know
        whether a trigger occurred must check the scheduler state
        (``last_triggered`` / ``next_due``), which advanced independently of
        the callback's output.
        """
        if now is None:
            now = _utcnow()
        if not self.is_due(now):
            return None
        self.mark_triggered(now)
        return self.run_callback()
