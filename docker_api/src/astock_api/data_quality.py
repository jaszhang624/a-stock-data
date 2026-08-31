"""Data Quality Audit: read-only validators for market_bars_daily.

Pure read layer — does NOT mutate state, does NOT create new tables.
Validates:
1. Identity integrity (canonical_id format EXCHANGE:CODE)
2. OHLCV data quality (negative prices, zero volume anomalies, etc.)
3. Duplicate detection (PK violations)
4. Completeness (instruments with no data)
5. Freshness improvement tracking

Output: data/reports/data_quality_report.json
"""

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _get_conn(store):
    """Get a fresh DuckDB connection from the store's path.

    Uses independent connection to avoid bootstrap() closing the shared _conn.
    """
    import duckdb

    return duckdb.connect(store.duckdb_path)


def validate_identity(store) -> dict:
    """Check that all security_id values follow EXCHANGE:CODE format.

    Args:
        store: DatasetStore instance.

    Returns:
        Dict with errors count and details.
    """
    conn = _get_conn(store)
    try:
        # Find rows where security_id doesn't match EXCHANGE:CODE pattern
        cur = conn.execute(
            "SELECT security_id, trade_date FROM market_bars_daily "
            "WHERE security_id NOT LIKE '%:%' OR security_id IS NULL"
        )
        bad_rows = cur.fetchall()

        # Also check for malformed exchange codes (should be uppercase letters)
        cur = conn.execute(
            "SELECT DISTINCT security_id FROM market_bars_daily "
            "WHERE security_id LIKE '%:%' AND LENGTH(SUBSTR(security_id, 1, INSTR(security_id, ':')-1)) < 2"
        )
        malformed_ids = [row[0] for row in cur.fetchall()]

        return {
            "errors": len(bad_rows),
            "bad_rows_sample": [{"security_id": r[0], "trade_date": str(r[1])} for r in bad_rows[:10]],
            "malformed_ids": malformed_ids,
        }
    finally:
        conn.close()


def validate_ohlcv(store) -> dict:
    """Check OHLCV data quality.

    Validates:
    - No negative prices (open, high, low, close)
    - High >= Low always holds
    - High >= Open and High >= Close
    - Low <= Open and Low <= Close
    - Volume is non-negative

    Args:
        store: DatasetStore instance.

    Returns:
        Dict with invalid_rows count and breakdown by issue type.
    """
    conn = _get_conn(store)
    try:
        # Negative prices
        cur = conn.execute(
            "SELECT security_id, trade_date FROM market_bars_daily "
            "WHERE open < 0 OR high < 0 OR low < 0 OR close < 0"
        )
        negative_prices = cur.fetchall()

        # High < Low (impossible)
        cur = conn.execute(
            "SELECT security_id, trade_date FROM market_bars_daily WHERE high < low"
        )
        hl_inversion = cur.fetchall()

        # High not max of OHLC
        cur = conn.execute(
            "SELECT security_id, trade_date FROM market_bars_daily "
            "WHERE (high < open) OR (high < close)"
        )
        high_not_max = cur.fetchall()

        # Low not min of OHLC
        cur = conn.execute(
            "SELECT security_id, trade_date FROM market_bars_daily "
            "WHERE (low > open) OR (low > close)"
        )
        low_not_min = cur.fetchall()

        # Negative volume
        cur = conn.execute(
            "SELECT security_id, trade_date FROM market_bars_daily WHERE volume < 0"
        )
        negative_volume = cur.fetchall()

        # Collect all unique invalid keys
        all_invalid = set()
        for row in negative_prices + hl_inversion + high_not_max + low_not_min + negative_volume:
            all_invalid.add((row[0], str(row[1])))

        return {
            "invalid_rows": len(all_invalid),
            "negative_prices": len(negative_prices),
            "hl_inversion": len(hl_inversion),
            "high_not_max": len(high_not_max),
            "low_not_min": len(low_not_min),
            "negative_volume": len(negative_volume),
        }
    finally:
        conn.close()


def validate_duplicates(store) -> dict:
    """Check for duplicate (security_id, trade_date) keys.

    Args:
        store: DatasetStore instance.

    Returns:
        Dict with duplicated_keys count and sample.
    """
    conn = _get_conn(store)
    try:
        cur = conn.execute(
            "SELECT security_id, trade_date, COUNT(*) as cnt FROM market_bars_daily "
            "GROUP BY security_id, trade_date HAVING cnt > 1"
        )
        duplicates = cur.fetchall()

        return {
            "duplicated_keys": len(duplicates),
            "sample": [{"security_id": r[0], "trade_date": str(r[1]), "count": r[2]} for r in duplicates[:10]],
        }
    finally:
        conn.close()


def validate_completeness(store, universe_path: str) -> dict:
    """Check how many instruments from the universe have no data.

    Args:
        store: DatasetStore instance.
        universe_path: Path to instrument_universe_v2.json.

    Returns:
        Dict with missing count and total universe size.
    """
    from astock_api.coverage import load_universe

    instruments = load_universe(universe_path)
    conn = _get_conn(store)
    try:
        # Get all security_ids that have data
        cur = conn.execute("SELECT DISTINCT security_id FROM market_bars_daily")
        existing_ids = {row[0] for row in cur.fetchall()}

        missing = [inst["canonical_id"] for inst in instruments if inst["canonical_id"] not in existing_ids]

        return {
            "total_universe": len(instruments),
            "with_data": len(existing_ids & {inst["canonical_id"] for inst in instruments}),
            "missing": len(missing),
            "missing_sample": missing[:10],
        }
    finally:
        conn.close()


def validate_freshness_improvement(store, universe_path: str, reference_date: str) -> dict:
    """Check freshness improvement compared to the given reference date.

    Args:
        store: DatasetStore instance.
        universe_path: Path to instrument_universe_v2.json.
        reference_date: YYYY-MM-DD date for freshness assessment.

    Returns:
        Dict with improved count and breakdown.
    """
    from astock_api.coverage import load_universe
    from astock_api.freshness import assess_freshness

    instruments = load_universe(universe_path)
    conn = _get_conn(store)
    try:
        # Get latest trade date per instrument
        cur = conn.execute(
            "SELECT security_id, MAX(trade_date) as latest FROM market_bars_daily GROUP BY security_id"
        )
        latest_dates = {row[0]: str(row[1]) for row in cur.fetchall()}

        improved = 0
        stale_count = 0
        current_count = 0
        missing_count = 0

        for inst in instruments:
            cid = inst["canonical_id"]
            latest = latest_dates.get(cid)
            status = assess_freshness(latest, reference_date)

            if status == "CURRENT":
                current_count += 1
                improved += 1
            elif status == "STALE":
                stale_count += 1
            else:
                missing_count += 1

        return {
            "improved": improved,
            "current": current_count,
            "stale": stale_count,
            "missing": missing_count,
        }
    finally:
        conn.close()


def generate_quality_report(
    store,
    universe_path: str,
    reference_date: str | None = None,
) -> dict:
    """Generate the complete data quality audit report.

    Args:
        store: DatasetStore instance.
        universe_path: Path to instrument_universe_v2.json.
        reference_date: YYYY-MM-DD date for freshness assessment (default: today).

    Returns:
        Complete data quality report dict.
    """
    if reference_date is None:
        reference_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    identity = validate_identity(store)
    ohlcv = validate_ohlcv(store)
    duplicates = validate_duplicates(store)
    completeness = validate_completeness(store, universe_path)
    freshness = validate_freshness_improvement(store, universe_path, reference_date)

    # Overall status: PASS if no critical errors
    overall = "PASS" if (identity["errors"] == 0 and ohlcv["invalid_rows"] == 0 and duplicates["duplicated_keys"] == 0) else "FAIL"

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reference_date": reference_date,
        "status": overall,
        "identity": identity,
        "ohlcv": ohlcv,
        "duplicates": duplicates,
        "completeness": completeness,
        "freshness": freshness,
    }

    logger.info(f"Data quality report: status={overall}")
    return report


def write_quality_report(report: dict, output_dir: str = "/app/data/reports") -> str:
    """Write the data quality report to disk.

    Args:
        report: The report dict from generate_quality_report().
        output_dir: Directory to write the artifact.

    Returns:
        Path to the written report file.
    """
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "data_quality_report.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2)

    logger.info(f"Data quality report written to {path}")
    return path


if __name__ == "__main__":
    import sys

    ref_date = sys.argv[1] if len(sys.argv) > 1 else None
    universe_path = sys.argv[2] if len(sys.argv) > 2 else "/app/data/universe/instrument_universe_v2.json"

    from astock_api.dataset_store import DatasetStore

    store = DatasetStore("/app/data/astock_data.duckdb")
    store.bootstrap()

    report = generate_quality_report(store, universe_path, ref_date)
    path = write_quality_report(report)

    print(json.dumps(report, indent=2))
    print(f"\nReport written to: {path}")
