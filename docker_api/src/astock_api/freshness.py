"""Freshness model: determine data freshness status for instruments.

Pure Python — no database access, no side effects.
"""


def assess_freshness(latest_trade_date: str | None, reference_date: str) -> str:
    """Assess freshness status given latest stored date and explicit reference.

    Args:
        latest_trade_date: YYYY-MM-DD string or None (no data).
        reference_date: Explicit YYYY-MM-DD reference date.

    Returns:
        "MISSING" if no data exists,
        "CURRENT" if latest >= reference_date,
        "STALE" if latest < reference_date.

    Does NOT guess weekends, holidays, or trading calendars.
    """
    if latest_trade_date is None:
        return "MISSING"
    return "CURRENT" if latest_trade_date >= reference_date else "STALE"
