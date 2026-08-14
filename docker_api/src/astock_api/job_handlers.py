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
    """
    symbol = str(payload["symbol"])
    frequency = payload.get("frequency", "daily")
    count = int(payload.get("count", 100))

    if frequency != "daily":
        raise PermanentJobError(
            f"market_bars_snapshot supports daily only: {frequency}"
        )

    # mootdx bars() expects the raw six-digit A-share code.
    if len(symbol) != 6 or not symbol.isdigit():
        raise PermanentJobError(
            f"market_bars_snapshot supports 6-digit numeric symbols only: {symbol}"
        )

    try:
        governor = get_governor()
        result = governor.fetch_market_bars(symbol, frequency, count)
    except GovernorUnavailableError as e:
        raise TransientJobError(
            f"all sources unavailable for {symbol}: {e}"
        ) from e
    except GovernorUnsupportedError as e:
        raise PermanentJobError(
            f"no source supports {symbol}: {e}"
        ) from e
    except Exception as e:
        raise TransientJobError(
            f"source governor failed for {symbol}: {e}"
        ) from e

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
    """
    from astock_api.dataset_store import DatasetStore

    symbol = str(payload["symbol"])
    frequency = payload.get("frequency", "daily")
    count = int(payload.get("count", 100))

    if frequency != "daily":
        raise PermanentJobError(
            f"market_bars_sync supports daily only: {frequency}"
        )

    if len(symbol) != 6 or not symbol.isdigit():
        raise PermanentJobError(
            f"market_bars_sync supports 6-digit numeric symbols only: {symbol}"
        )

    # Step 1: Acquire via Source Governor (reuse existing capability)
    try:
        governor = get_governor()
        result = governor.fetch_market_bars(symbol, frequency, count)
    except GovernorUnavailableError as e:
        raise TransientJobError(f"all sources unavailable for {symbol}: {e}") from e
    except GovernorUnsupportedError as e:
        raise PermanentJobError(f"no source supports {symbol}: {e}") from e
    except Exception as e:
        raise TransientJobError(f"source governor failed for {symbol}: {e}") from e

    # Step 2: Normalize to canonical format
    security_id = DatasetStore.make_security_id(symbol)

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
