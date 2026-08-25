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

State is held in memory only — no database, no config files, no daemon thread
yet. The caller supplies the real cycle as ``run_callback`` and drives
``run_once()`` from whatever loop it chooses.
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
        """If due: call ``run_callback()`` then ``mark_triggered()``.

        Returns the callback's execution result when it ran, or ``None`` when
        the scheduler was not due (or disabled).
        """
        if now is None:
            now = _utcnow()
        if not self.is_due(now):
            return None
        result = self.run_callback()
        self.mark_triggered(now)
        return result
