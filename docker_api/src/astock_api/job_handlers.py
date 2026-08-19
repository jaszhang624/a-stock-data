"""Job handlers: map job_type to actual upstream execution."""

import logging

from astock_api.job_engine import TransientJobError, PermanentJobError
from astock_api.source_governor import (
    get_governor,
    GovernorUnavailableError,
    GovernorUnsupportedError,
)

logger = logging.getLogger(__name__)


def market_bars_handler(payload: dict):
    """Handle one daily market-bars snapshot chunk via SourceGovernor.

    payload: {"symbol": "600519", "frequency": "daily", "count": 100}
    R5-C2: payload may include "chunk_id" for checkpoint persistence.
    C4B-2: bare symbol is parsed to Instrument (EQUITY context) before governor.
    """
    from astock_api.instrument import parse_instrument

    symbol = str(payload["symbol"])
    frequency = payload.get("frequency", "daily")
    count = int(payload.get("count", 100))
    chunk_id = payload.get("chunk_id")

    if frequency != "daily":
        raise PermanentJobError(
            f"market_bars_snapshot supports daily only: {frequency}"
        )

    # mootdx bars() expects the raw six-digit A-share code.
    if len(symbol) != 6 or not symbol.isdigit():
        raise PermanentJobError(
            f"market_bars_snapshot supports 6-digit numeric symbols only: {symbol}"
        )

    # C4B-2: Parse bare symbol to Instrument in EQUITY context.
    # Existing equity workflow: 600519 → SSE:600519, 000001 → SZSE:000001
    instrument = parse_instrument(symbol, asset_type="EQUITY")

    try:
        governor = get_governor()
        result = governor.fetch_market_bars(instrument, frequency, count)
    except GovernorUnavailableError as e:
        # R5-C2: save checkpoints for each source error before re-raising
        if chunk_id and e.source_errors:
            _save_checkpoints_from_errors(chunk_id, e.source_errors)
        raise TransientJobError(
            f"all sources unavailable for {symbol}: {e}"
        ) from e
    except GovernorUnsupportedError as e:
        # R5-C2: save checkpoints for each source error before re-raising
        if chunk_id and e.source_errors:
            _save_checkpoints_from_errors(chunk_id, e.source_errors)
        raise PermanentJobError(
            f"no source supports {symbol}: {e}"
        ) from e
    except Exception as e:
        raise TransientJobError(
            f"source governor failed for {symbol}: {e}"
        ) from e

    # R5-C2: save checkpoint for successful source
    if chunk_id and isinstance(result, dict):
        _save_source_checkpoint(chunk_id, result.get("source", "unknown"), "DATA_OK")

    return result


def market_bars_sync_handler(payload: dict):
    """Handle one daily market-bars sync chunk via SourceGovernor + DuckDB canonical write.

    Crash-safe ordering:
    1. acquire via Source Governor
    2. normalize to canonical format
    3. atomic artifact write (Job Engine)
    4. DuckDB BEGIN → UPSERT → COMMIT
    5. mark chunk DONE (Job Engine)

    payload: {"symbol": "600519", "frequency": "daily", "count": 100}
    R5-C2: payload may include "chunk_id" for checkpoint persistence.
    C4B-2: bare symbol is parsed to Instrument (EQUITY context) before governor.
    """
    from astock_api.dataset_store import DatasetStore
    from astock_api.instrument import parse_instrument

    symbol = str(payload["symbol"])
    frequency = payload.get("frequency", "daily")
    count = int(payload.get("count", 100))
    chunk_id = payload.get("chunk_id")

    if frequency != "daily":
        raise PermanentJobError(
            f"market_bars_sync supports daily only: {frequency}"
        )

    if len(symbol) != 6 or not symbol.isdigit():
        raise PermanentJobError(
            f"market_bars_sync supports 6-digit numeric symbols only: {symbol}"
        )

    # C4B-2: Parse bare symbol to Instrument in EQUITY context.
    instrument = parse_instrument(symbol, asset_type="EQUITY")

    # Step 1: Acquire via Source Governor (reuse existing capability)
    try:
        governor = get_governor()
        result = governor.fetch_market_bars(instrument, frequency, count)
    except GovernorUnavailableError as e:
        # R5-C2: save checkpoints for each source error before re-raising
        if chunk_id and e.source_errors:
            _save_checkpoints_from_errors(chunk_id, e.source_errors)
        raise TransientJobError(f"all sources unavailable for {symbol}: {e}") from e
    except GovernorUnsupportedError as e:
        # R5-C2: save checkpoints for each source error before re-raising
        if chunk_id and e.source_errors:
            _save_checkpoints_from_errors(chunk_id, e.source_errors)
        raise PermanentJobError(f"no source supports {symbol}: {e}") from e
    except Exception as e:
        raise TransientJobError(f"source governor failed for {symbol}: {e}") from e

    # Step 2: Use Instrument canonical_id as security_id (aligns with DatasetStore)
    security_id = instrument.canonical_id

    # Source Governor returns normalized rows with keys:
    # datetime, open, high, low, close, volume (手), amount (元)
    # Volume canonical unit: 手 (lots of 100 shares) — same as mootdx native
    # Amount canonical unit: 元 (yuan)
    bars = []
    if isinstance(result, dict):
        rows = result.get('rows', [])
        for bar in rows:
            bars.append({
                "trade_date": str(bar.get('datetime', ''))[:10],  # YYYY-MM-DD
                "open": bar.get('open'),
                "high": bar.get('high'),
                "low": bar.get('low'),
                "close": bar.get('close'),
                "volume": int(bar.get('volume', 0)),   # 手 (lots of 100)
                "amount": float(bar.get('amount', 0)), # 元 (yuan)
            })

    # Step 3: Write to DuckDB canonical store (idempotent UPSERT)
    store = DatasetStore('/app/data/astock_data.duckdb')
    store.bootstrap()  # Ensure tables exist

    # Write bars with crash-safe transaction (BEGIN → UPSERT → COMMIT)
    store.write_market_bars(security_id, bars, 'mootdx', payload.get('job_id', ''))

    # R5-C3: DATA_OK checkpoint AFTER DuckDB commit — prevents data loss if crash
    # occurs between source fetch and DuckDB write. If crash happens in the window,
    # restart will re-fetch + idempotent UPSERT (at-least-once semantics).
    if chunk_id and isinstance(result, dict):
        _save_source_checkpoint(chunk_id, result.get("source", "unknown"), "DATA_OK")

    return {"security_id": security_id, "bars_count": len(bars)}


def get_handler(job_type: str):
    """Get handler function for job type."""
    from astock_api.security_master_handler import security_master_snapshot_handler

    handlers = {
        "market_bars_snapshot": market_bars_handler,
        "market_bars_sync": market_bars_sync_handler,
        "security_master_snapshot": security_master_snapshot_handler,
    }
    return handlers.get(job_type)


# ── R5-C2: Checkpoint helpers ───────────────────────────────────────

from astock_api.source_adapters import (
    SourceTransientError,
    SourceUnsupportedError,
    SourceDataError,
)


def _save_source_checkpoint(chunk_id: str, provider: str, outcome: str) -> None:
    """Save a source checkpoint via the job engine."""
    from astock_api.main import get_engine
    try:
        engine = get_engine()
        engine._save_source_checkpoint(chunk_id, provider, outcome, completed=True)
    except Exception:
        pass  # Best-effort; don't fail the chunk for checkpoint issues


def _save_checkpoints_from_errors(chunk_id: str, source_errors: list) -> None:
    """Save checkpoints from governor source_errors.

    Maps SourceError types to outcomes and determines completion status.
    """
    from astock_api.main import get_engine
    try:
        engine = get_engine()
    except Exception:
        return  # Best-effort

    for err in source_errors:
        if isinstance(err, SourceUnsupportedError):
            engine._save_source_checkpoint(chunk_id, err.source_name if hasattr(err, 'source_name') else 'unknown', "UNSUPPORTED", completed=True, error=str(err))
        elif isinstance(err, SourceDataError):
            # definitive=True → completed; definitive=False → not completed (may retry)
            engine._save_source_checkpoint(chunk_id, err.source_name if hasattr(err, 'source_name') else 'unknown', "DATA_ERROR", completed=err.definitive if hasattr(err, 'definitive') else True, error=str(err))
        elif isinstance(err, SourceTransientError):
            # Transient errors are NOT completed — the provider may succeed on retry.
            engine._save_source_checkpoint(chunk_id, err.source_name if hasattr(err, 'source_name') else 'unknown', "TRANSIENT", completed=False, error=str(err))
