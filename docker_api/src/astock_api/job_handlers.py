"""Job handlers: map job_type to actual upstream execution."""

import logging

from astock_api.job_engine import TransientJobError, PermanentJobError

logger = logging.getLogger(__name__)


def market_bars_handler(payload: dict):
    """Handle one daily market-bars snapshot chunk via mootdx.

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
        from astock_api.upstream.common import tdx_client

        client = tdx_client()
        df = client.bars(symbol, frequency=9, offset=count)
    except Exception as e:
        raise TransientJobError(
            f"mootdx bars failed for {symbol}: {e}"
        ) from e

    if df is None or not hasattr(df, "empty") or df.empty:
        raise TransientJobError(
            f"mootdx returned empty bars for {symbol}"
        )

    columns = [
        "datetime",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    ]

    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise PermanentJobError(
            f"mootdx bars schema missing columns: {','.join(missing)}"
        )

    rows = df.tail(count)[columns].to_dict(orient="records")

    return {
        "source": "mootdx",
        "symbol": symbol,
        "frequency": "daily",
        "requested_count": count,
        "rows": rows,
    }


def get_handler(job_type: str):
    """Get handler function for job type."""
    handlers = {
        "market_bars_snapshot": market_bars_handler,
    }
    return handlers.get(job_type)
