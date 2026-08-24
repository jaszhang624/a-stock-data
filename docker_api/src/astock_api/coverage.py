"""Coverage snapshot: generate market data coverage report from Universe v2.

Uses batch SQL queries — avoids N+1 database access pattern.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def load_universe(universe_path: str) -> list[dict]:
    """Load Universe v2 instruments from JSON artifact."""
    with open(universe_path, "r") as f:
        data = json.load(f)
    # Universe v2 is a list of instrument dicts
    if isinstance(data, list):
        return data
    return data.get("instruments", [])


def _load_instruments(store, universe_path: str):
    """Load instruments from Security Master (primary) or JSON (fallback).

    Returns (instruments, source_label).
    - source_label = 'security_master' when SM is ACTIVE
    - source_label = 'universe_json' on fallback

    Security Master rows arrive already universe-shaped from
    DatasetStore.get_active_security_master() (canonical_id, code,
    exchange, asset_type, name) — passed through unchanged.
    """
    try:
        sm_instruments = store.get_active_security_master()
    except Exception as e:
        logger.warning(f"Security Master read failed, falling back to JSON: {e}")
        sm_instruments = None

    if sm_instruments:
        return sm_instruments, "security_master"

    return load_universe(universe_path), "universe_json"


def generate_coverage_snapshot(
    store,
    universe_path: str,
    reference_date: str,
    output_dir: str = "/app/data/coverage",
) -> dict:
    """Generate coverage snapshot for all Universe v2 instruments.

    Args:
        store: DatasetStore instance (must have get_all_instrument_states).
        universe_path: Path to instrument_universe_v2.json (fallback source).
        reference_date: Explicit YYYY-MM-DD reference date for freshness.
        output_dir: Directory to write coverage artifact.

    Returns:
        Coverage snapshot dict with per-instrument state and aggregates.
    """
    from astock_api.freshness import assess_freshness

    # Load instruments: Security Master primary, JSON universe fallback
    instruments, source_label = _load_instruments(store, universe_path)
    logger.info(f"Coverage instruments source: {source_label} ({len(instruments)} instruments)")

    # Batch query: get ALL stored states in one SQL call (avoids N+1)
    all_states = store.get_all_instrument_states()
    state_by_id = {s["security_id"]: s for s in all_states}

    # Build per-instrument coverage records
    instrument_records = []
    for inst in instruments:
        canonical_id = inst["canonical_id"]
        exchange = inst.get("exchange", "")
        asset_type = inst.get("asset_type", "EQUITY")

        state = state_by_id.get(canonical_id)
        if state is None:
            # No data for this instrument — MISSING
            record = {
                "canonical_id": canonical_id,
                "exchange": exchange,
                "asset_type": asset_type,
                "row_count": 0,
                "earliest_trade_date": None,
                "latest_trade_date": None,
                "freshness_status": "MISSING",
            }
        else:
            freshness = assess_freshness(state["latest_trade_date"], reference_date)
            record = {
                "canonical_id": canonical_id,
                "exchange": exchange,
                "asset_type": asset_type,
                "row_count": state["row_count"],
                "earliest_trade_date": state["earliest_trade_date"],
                "latest_trade_date": state["latest_trade_date"],
                "freshness_status": freshness,
            }

        instrument_records.append(record)

    # Aggregate counts
    total = len(instrument_records)
    with_data = sum(1 for r in instrument_records if r["row_count"] > 0)
    missing = sum(1 for r in instrument_records if r["freshness_status"] == "MISSING")
    current = sum(1 for r in instrument_records if r["freshness_status"] == "CURRENT")
    stale = sum(1 for r in instrument_records if r["freshness_status"] == "STALE")

    # Breakdown by asset_type
    equity_records = [r for r in instrument_records if r["asset_type"] == "EQUITY"]
    index_records = [r for r in instrument_records if r["asset_type"] == "INDEX"]

    equity_summary = {
        "total": len(equity_records),
        "with_data": sum(1 for r in equity_records if r["row_count"] > 0),
        "missing": sum(1 for r in equity_records if r["freshness_status"] == "MISSING"),
        "current": sum(1 for r in equity_records if r["freshness_status"] == "CURRENT"),
        "stale": sum(1 for r in equity_records if r["freshness_status"] == "STALE"),
    }

    index_summary = {
        "total": len(index_records),
        "with_data": sum(1 for r in index_records if r["row_count"] > 0),
        "missing": sum(1 for r in index_records if r["freshness_status"] == "MISSING"),
        "current": sum(1 for r in index_records if r["freshness_status"] == "CURRENT"),
        "stale": sum(1 for r in index_records if r["freshness_status"] == "STALE"),
        "note": "supported_seed_indices_only",
    }

    # Breakdown by exchange
    sse_records = [r for r in instrument_records if r["exchange"] == "SSE"]
    szse_records = [r for r in instrument_records if r["exchange"] == "SZSE"]

    sse_summary = {
        "total": len(sse_records),
        "with_data": sum(1 for r in sse_records if r["row_count"] > 0),
        "missing": sum(1 for r in sse_records if r["freshness_status"] == "MISSING"),
        "current": sum(1 for r in sse_records if r["freshness_status"] == "CURRENT"),
        "stale": sum(1 for r in sse_records if r["freshness_status"] == "STALE"),
    }

    szse_summary = {
        "total": len(szse_records),
        "with_data": sum(1 for r in szse_records if r["row_count"] > 0),
        "missing": sum(1 for r in szse_records if r["freshness_status"] == "MISSING"),
        "current": sum(1 for r in szse_records if r["freshness_status"] == "CURRENT"),
        "stale": sum(1 for r in szse_records if r["freshness_status"] == "STALE"),
    }

    # Read universe manifest hash if available
    manifest_path = str(Path(universe_path).with_suffix(".manifest.json"))
    universe_sha256 = None
    try:
        with open(manifest_path, "r") as f:
            manifest = json.load(f)
        universe_sha256 = manifest.get("artifact_sha256")
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Build snapshot
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reference_date": reference_date,
        "universe_source": source_label,
        "universe_manifest_hash": universe_sha256,
        "summary": {
            "total_universe": total,
            "with_data": with_data,
            "missing": missing,
            "current": current,
            "stale": stale,
        },
        "breakdown": {
            "equity": equity_summary,
            "index_seed": index_summary,
            "sse": sse_summary,
            "szse": szse_summary,
        },
        "instruments": instrument_records,
    }

    # Write artifact
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    artifact_file = out_path / "market_bars_daily_coverage.json"
    with open(artifact_file, "w") as f:
        json.dump(snapshot, f, indent=2)

    logger.info(f"Coverage snapshot written to {artifact_file}")
    return snapshot


def get_coverage_summary(
    store,
    universe_path: str,
    reference_date: str,
) -> dict:
    """Return just the aggregate coverage summary (no per-instrument detail).

    Useful for lightweight API endpoints.
    """
    snapshot = generate_coverage_snapshot(store, universe_path, reference_date)
    # Return summary without per-instrument records
    return {k: v for k, v in snapshot.items() if k != "instruments"}


if __name__ == "__main__":
    import sys

    ref_date = sys.argv[1] if len(sys.argv) > 1 else "2026-08-20"
    universe_path = sys.argv[2] if len(sys.argv) > 2 else "/app/data/universe/instrument_universe_v2.json"

    from astock_api.dataset_store import DatasetStore
    store = DatasetStore("/app/data/astock_data.duckdb")
    store.bootstrap()

    snapshot = generate_coverage_snapshot(store, universe_path, ref_date)
    print(json.dumps(snapshot["summary"], indent=2))
    print(f"\nTotal instruments: {snapshot['summary']['total_universe']}")
    print(f"With data: {snapshot['summary']['with_data']}")
    print(f"Missing: {snapshot['summary']['missing']}")
    print(f"Current: {snapshot['summary']['current']}")
    print(f"Stale: {snapshot['summary']['stale']}")
