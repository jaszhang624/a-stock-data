"""Source Governor: routes market bars requests to registered sources.

Translates source-level errors (source_adapters) into Governor-level errors
for the job handler layer.
"""

import threading as _threading
import time as _time_module
from typing import Any

from astock_api.source_adapters import (
    MarketBarsSource,
    SourceError,
    SourceTransientError,
    SourceUnsupportedError,
    SourceDataError,
)


class GovernorUnavailableError(Exception):
    """All registered sources are unavailable for this request."""

    def __init__(self, message: str, source_errors: list[SourceError] | None = None):
        super().__init__(message)
        self.source_errors = source_errors or []


class GovernorUnsupportedError(Exception):
    """No registered source supports this symbol/market."""

    def __init__(self, message: str, source_errors: list[SourceError] | None = None):
        super().__init__(message)
        self.source_errors = source_errors or []


MARKET_BARS_CAPABILITY = "market_bars_daily"


class SourceGovernor:
    """Routes market bars requests to registered sources.

    v6: mootdx (primary) → Baidu (secondary).
    In-memory health counters with CLOSED/OPEN/HALF_OPEN circuit breaker.
    OPEN threshold: 2 consecutive failures (SourceTransientError/SourceDataError).
    Cooldown: 30s after OPEN before allowing a HALF_OPEN probe.
    HALF_OPEN: single recovery probe; other requests skip and fallback.
    SourceUnsupportedError does NOT trigger OPEN.

    Optional state_store parameter persists health state to SQLite.
    """

    OPEN_THRESHOLD = 2
    OPEN_COOLDOWN_SECONDS = 30

    def __init__(self, now_fn=None, state_store=None):
        self._sources: dict[str, MarketBarsSource] = {}
        # In-memory health state per source name.
        self._health: dict[str, dict] = {}
        # Per-source locks for thread-safe state transitions.
        self._locks: dict[str, _threading.Lock] = {}
        # Clock function — injectable for testing.
        self.now_fn = now_fn or _time_module.time
        # Optional persistent state store.
        self._state_store = state_store

    def register(self, source: MarketBarsSource) -> None:
        """Register a data source by its name."""
        self._sources[source.name] = source
        self._locks[source.name] = _threading.Lock()

        # Load from store or initialize default.
        if self._state_store is not None:
            stored = self._state_store.load(source.name, MARKET_BARS_CAPABILITY)
            if stored is not None:
                state = stored["state"]

                # HALF_OPEN restart recovery — no active probe after crash.
                if state == "HALF_OPEN":
                    self._health[source.name] = {
                        "consecutive_failures": stored["consecutive_failures"],
                        "state": "OPEN",
                        "open_until": self.now_fn() + self.OPEN_COOLDOWN_SECONDS,
                    }
                    # Persist the recovery state.
                    self._state_store.save(
                        source.name, MARKET_BARS_CAPABILITY, self._health[source.name]
                    )
                else:
                    # CLOSED or OPEN — load as-is.
                    self._health[source.name] = {
                        "consecutive_failures": stored["consecutive_failures"],
                        "state": state,
                        "open_until": stored["open_until"],
                    }
            else:
                self._health[source.name] = {
                    "consecutive_failures": 0,
                    "state": "CLOSED",
                    "open_until": None,
                }
        else:
            self._health[source.name] = {
                "consecutive_failures": 0,
                "state": "CLOSED",
                "open_until": None,
            }

    def get_source_health(self, source_name: str) -> dict | None:
        """Return health state for a registered source (for testing/observability)."""
        return self._health.get(source_name)

    def get_health_snapshot(self) -> list[dict]:
        """Return a read-only snapshot of all source health states.

        Returns sources in registration order (priority order).
        Does not call any upstream adapters or modify state.
        """
        snapshot = []
        for source in self._sources.values():
            health = self._health.get(source.name)
            if health is not None:
                snapshot.append({
                    "source": source.name,
                    "capability": MARKET_BARS_CAPABILITY,
                    "state": health["state"],
                    "consecutive_failures": health["consecutive_failures"],
                    "open_until": health["open_until"],
                })
        return snapshot

    def _persist(self, name: str) -> None:
        """Persist current health state to store if available."""
        if self._state_store is not None:
            self._state_store.save(name, MARKET_BARS_CAPABILITY, self._health[name])

    def _set_open(self, name: str) -> None:
        """Set a source to OPEN with cooldown."""
        self._health[name]["state"] = "OPEN"
        self._health[name]["open_until"] = self.now_fn() + self.OPEN_COOLDOWN_SECONDS

    def _set_closed(self, name: str) -> None:
        """Reset a source to CLOSED."""
        self._health[name]["consecutive_failures"] = 0
        self._health[name]["state"] = "CLOSED"
        self._health[name]["open_until"] = None

    def fetch_market_bars(
        self, instrument: Any, frequency: str, count: int
    ) -> dict:
        """Fetch market bars from the first available source.

        Args:
            instrument: Instrument object with exchange, code, asset_type.
                Passed through to adapters for provider-specific conversion.
            frequency: 'daily' (only supported value).
            count: Number of bars to return.

        Returns:
            Result dict with source, symbol, canonical_id, exchange, frequency, requested_count, rows.

        Raises:
            GovernorUnavailableError: all sources returned transient/data errors or are OPEN/HALF_OPEN.
            GovernorUnsupportedError: no source supports this symbol/market.

        C4B-2: Instrument identity is preserved through the governor chain.
        The governor does NOT re-infer exchange from code prefix.
        """
        errors: list[SourceError] = []

        for source in self._sources.values():
            name = source.name
            health = self._health[name]
            lock = self._locks[name]

            # ── State transition check (thread-safe) ─────────────
            with lock:
                state = health["state"]

                if state == "HALF_OPEN":
                    # Another request is already probing — skip.
                    err = SourceTransientError(
                        f"{name} is HALF_OPEN (probe in progress)"
                    )
                    err.source_name = name
                    errors.append(err)
                    continue

                if state == "OPEN":
                    # Cooldown expired — transition to HALF_OPEN for probe.
                    if health["open_until"] is not None and self.now_fn() >= health["open_until"]:
                        health["state"] = "HALF_OPEN"
                        self._persist(name)
                    else:
                        # Still in cooldown — skip adapter, treat as unavailable.
                        err = SourceTransientError(
                            f"{name} is OPEN (consecutive_failures={health['consecutive_failures']})"
                        )
                        err.source_name = name
                        errors.append(err)
                        continue

            # ── Call adapter (outside lock) ───────────────────────
            try:
                result = source.fetch_market_bars(instrument, frequency, count)

                # ── Write probe result (thread-safe) ─────────────
                with lock:
                    self._set_closed(name)
                    self._persist(name)

                return result

            except SourceUnsupportedError as e:
                # Unsupported does NOT count as a health failure.
                if not e.source_name:
                    e.source_name = name
                with lock:
                    if health["state"] == "HALF_OPEN":
                        # Probe was unsupported — back to OPEN.
                        self._set_open(name)
                        self._persist(name)
                errors.append(e)

            except SourceTransientError as e:
                # Transient error — increment consecutive failures.
                if not e.source_name:
                    e.source_name = name
                with lock:
                    health["consecutive_failures"] += 1
                    if health["consecutive_failures"] >= self.OPEN_THRESHOLD:
                        self._set_open(name)
                    self._persist(name)
                errors.append(e)

            except SourceDataError as e:
                # Data error — does NOT trigger circuit breaker.
                if not e.source_name:
                    e.source_name = name
                with lock:
                    if health["state"] == "HALF_OPEN":
                        # Probe succeeded in reaching the source — data issue is not a health concern.
                        self._set_closed(name)
                        self._persist(name)
                errors.append(e)

        # All sources failed — distinguish unsupported vs unavailable.
        # GovernorUnsupportedError if ALL errors are non-transient (unsupported or definitive data errors).
        # Only GovernorUnavailableError if at least one source returned a transient error.
        has_transient = any(
            isinstance(e, SourceTransientError) for e in errors
        )

        if not has_transient:
            # All sources returned unsupported or data errors — symbol simply has no data.
            # This is a permanent condition; retrying will not help.
            raise GovernorUnsupportedError(
                f"no data available for symbol {instrument.code}: {[str(e) for e in errors]}",
                source_errors=errors,
            )

        # Has transient errors — check if any source gave a definitive "no data" answer.
        # mootdx EMPTY (definitive) + baidu OPEN → GovernorUnavailableError (WAITING_SOURCE).
        # The mootdx answer is authoritative, but baidu OPEN means we haven't checked baidu yet.
        # Don't permanently fail; wait for baidu cooldown to expire and retry.
        raise GovernorUnavailableError(
            f"all sources unavailable for {instrument.code}: {[str(e) for e in errors]}",
            source_errors=errors,
        )


def create_governor(now_fn=None, state_store=None) -> SourceGovernor:
    """Create a governor with default sources.

    Priority: mootdx (primary) → Baidu (secondary).
    Each call returns a NEW instance — safe for tests.

    Args:
        now_fn: Optional clock function for testing.
        state_store: Optional SQLiteSourceStateStore for persistence.
    """
    from astock_api.source_adapters import MootdxSource, BaiduSource

    governor = SourceGovernor(now_fn=now_fn, state_store=state_store)
    governor.register(MootdxSource())
    governor.register(BaiduSource())
    return governor


# ── Process-level shared instance (lazy, thread-safe) ─────────────

_DEFAULT_DB_PATH = "/app/data/astock_source_governor.db"
_default_governor: SourceGovernor | None = None
_default_governor_lock = _threading.Lock()


def get_governor() -> SourceGovernor:
    """Return the process-level shared governor instance.

    Lazy initialization — creates the persistent governor on first call
    with SQLite state store at /app/data/astock_source_governor.db.

    Thread-safe: double-check locking ensures exactly one initialization
    even when multiple threads call get_governor() simultaneously.

    Reuses the same SourceGovernor across handler calls so that
    health state (consecutive_failures, OPEN/CLOSED) survives between requests.

    For tests that need isolation, use create_governor() instead.
    """
    global _default_governor

    if _default_governor is None:
        with _default_governor_lock:
            if _default_governor is None:
                from astock_api.source_state_store import SQLiteSourceStateStore

                store = SQLiteSourceStateStore(_DEFAULT_DB_PATH)
                _default_governor = create_governor(state_store=store)

    return _default_governor
