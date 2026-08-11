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


def get_handler(job_type: str):
    """Get handler function for job type."""
    handlers = {
        "market_bars_snapshot": market_bars_handler,
    }
    return handlers.get(job_type)
