"""Reconcile R5 historical snapshot results into DatasetStore.

Reads DONE chunk result files from the jobs directory, validates identity
and row structure, and upserts into market_bars_daily idempotently.

Usage:
    python -m astock_api.reconcile import-r5-snapshots --dry-run
    python -m astock_api.reconcile import-r5-snapshots

Do NOT modify historical result files. This is one-way reconciliation
into DatasetStore only.
"""

import argparse
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def resolve_equity_security_id(symbol: str) -> str | None:
    """Resolve bare R5 symbol to canonical security_id using EQUITY context.

    R5-C4A results are EQUITY-only. Use prefix inference:
      6xxxx → SSE, 0xxxx/3xxxx → SZSE

    Returns None for non-equity symbols (e.g., index codes).
    """
    from astock_api.dataset_store import DatasetStore

    try:
        return DatasetStore.make_security_id(symbol)
    except ValueError:
        return None


def parse_result_file(filepath: str) -> dict | None:
    """Parse a R5 snapshot result file.

    Returns parsed data dict or None if invalid/missing.
    """
    try:
        with open(filepath, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    if not isinstance(data, dict) or "data" not in data:
        return None

    inner = data.get("data")
    if not isinstance(inner, dict):
        return None

    rows = inner.get("rows", [])
    if not isinstance(rows, list) or len(rows) == 0:
        return None

    # Validate first row has required fields
    sample = rows[0]
    if not isinstance(sample, dict):
        return None
    required = {"datetime", "open", "high", "low", "close", "volume"}
    if not required.issubset(sample.keys()):
        return None

    return data


def discover_done_chunks(jobs_db_path: str, path_prefix_map: dict[str, str]) -> list[dict]:
    """Discover DONE chunks from jobs DB and map to actual file paths.

    Args:
        jobs_db_path: Path to astock_jobs.db (SQLite)
        path_prefix_map: Dict mapping stored prefix → actual prefix
                         e.g., {"/app/data/": "/data/"}

    Returns:
        List of dicts with chunk_key, stored_path, actual_path.
    """
    conn = sqlite3.connect(jobs_db_path)
    cursor = conn.execute("""
        SELECT c.chunk_key, c.result_path
        FROM job_chunks c
        JOIN jobs j ON c.job_id = j.job_id
        WHERE j.job_type='market_bars_snapshot' AND c.status='DONE'
    """)

    chunks = []
    for chunk_key, stored_path in cursor.fetchall():
        actual_path = stored_path
        for old_prefix, new_prefix in path_prefix_map.items():
            actual_path = actual_path.replace(old_prefix, new_prefix)

        chunks.append({
            "chunk_key": chunk_key,
            "stored_path": stored_path,
            "actual_path": actual_path,
        })

    conn.close()
    return chunks


def dry_run(chunks: list[dict]) -> dict:
    """Dry-run reconciliation without writing to DatasetStore.

    Returns inventory report with counts and classifications.
    """
    importable = 0
    total_rows = 0
    invalid_results = 0
    missing_results = 0
    identity_errors = 0
    empty_results = 0

    import_candidates = []

    for chunk in chunks:
        actual_path = chunk["actual_path"]
        if not os.path.exists(actual_path):
            missing_results += 1
            continue

        data = parse_result_file(actual_path)
        if data is None:
            invalid_results += 1
            continue

        inner = data["data"]
        symbol = inner.get("symbol", "")
        if not symbol:
            identity_errors += 1
            continue

        security_id = resolve_equity_security_id(symbol)
        if security_id is None:
            identity_errors += 1
            continue

        rows = inner.get("rows", [])
        if len(rows) == 0:
            empty_results += 1
            continue

        importable += 1
        total_rows += len(rows)
        import_candidates.append({
            "security_id": security_id,
            "symbol": symbol,
            "rows": rows,
            "source": inner.get("source", "mootdx"),
            "path": actual_path,
        })

    # Deduplicate: keep only the result with most rows per security_id
    by_security = {}
    for candidate in import_candidates:
        sid = candidate["security_id"]
        if sid not in by_security or len(candidate["rows"]) > len(by_security[sid]["rows"]):
            by_security[sid] = candidate

    return {
        "import_candidate_instruments": len(by_security),
        "import_candidate_rows": sum(len(v["rows"]) for v in by_security.values()),
        "invalid_results": invalid_results,
        "missing_results": missing_results,
        "identity_errors": identity_errors,
        "empty_results": empty_results,
        "import_candidates": by_security,
    }


def execute_import(store, import_candidates: dict[str, dict]) -> dict:
    """Execute idempotent batch import into DatasetStore.

    Uses DuckDB appender for bulk upsert instead of row-by-row INSERT.
    """
    import duckdb

    imported_instruments = 0
    imported_rows = 0

    # Use a stable job_id for reconciliation provenance
    job_id = "r6-4-reconciliation"

    # Open a dedicated connection for batch writes
    conn = duckdb.connect(store.duckdb_path)
    now = store._now_iso()

    # Collect ALL bars across all securities into one batch
    all_rows = []
    for security_id, candidate in import_candidates.items():
        rows = candidate["rows"]
        source = candidate.get("source", "mootdx")

        for row in rows:
            dt_str = str(row.get("datetime", ""))[:10]  # "2026-08-12"
            all_rows.append((
                security_id, dt_str,
                row.get("open"), row.get("high"), row.get("low"), row.get("close"),
                int(row.get("volume", 0)), float(row.get("amount", 0)),
                source, now, job_id,
            ))

    logger.info(f"Collected {len(all_rows)} rows for batch import")

    # Create temp table
    conn.execute("""
        CREATE TEMP TABLE import_batch (
            security_id TEXT,
            trade_date TEXT,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume BIGINT,
            amount DOUBLE,
            source TEXT,
            ingested_at TEXT,
            job_id TEXT
        )
    """)

    # Write to temp CSV for fast DuckDB COPY
    import tempfile
    import csv
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, newline='') as f:
        writer = csv.writer(f)
        writer.writerows(all_rows)
        tmp_csv = f.name

    logger.info(f"Wrote {len(all_rows)} rows to temp CSV")

    # Fast bulk load via COPY
    conn.execute(f"COPY import_batch FROM '{tmp_csv}' (AUTO_DETECT)")

    logger.info(f"Loaded {len(all_rows)} rows to temp table via COPY")

    # Upsert from temp table into target
    conn.execute("""
        INSERT INTO market_bars_daily
            (security_id, trade_date, open, high, low, close, volume, amount, source, ingested_at, job_id)
        SELECT security_id, trade_date, open, high, low, close, volume, amount, source, ingested_at, job_id
        FROM import_batch
        ON CONFLICT (security_id, trade_date)
        DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume, amount=excluded.amount,
            ingested_at=excluded.ingested_at
    """)

    conn.commit()
    imported_instruments = len(import_candidates)
    imported_rows = len(all_rows)

    logger.info(f"Batch import complete: {imported_instruments} instruments, "
                f"{imported_rows} rows in single transaction")

    conn.close()

    return {
        "imported_instruments": imported_instruments,
        "imported_rows": imported_rows,
    }


def get_datasetstore_stats(store) -> dict:
    """Get current DatasetStore instrument and row counts.

    Uses a separate connection to avoid closing the shared one.
    """
    import duckdb
    conn = duckdb.connect(store.duckdb_path)
    result = conn.execute("""
        SELECT COUNT(*), COUNT(DISTINCT security_id) FROM market_bars_daily
    """).fetchone()
    conn.close()
    return {
        "total_rows": result[0],
        "distinct_instruments": result[1],
    }


def main():
    parser = argparse.ArgumentParser(description="R5 snapshot reconciliation")
    subparsers = parser.add_subparsers(dest="command")

    import_parser = subparsers.add_parser("import-r5-snapshots", help="Import R5 snapshots")
    import_parser.add_argument("--dry-run", action="store_true", help="Only report, do not import")
    import_parser.add_argument("--jobs-db", default="/data/astock_jobs.db", help="Path to jobs DB")
    import_parser.add_argument("--store-db", default="/app/data/astock_data.duckdb", help="Path to DatasetStore")
    import_parser.add_argument("--path-prefix-map", default="/app/data/:/data/", help="Stored→actual path prefix mapping (comma-separated)")
    import_parser.add_argument("--output-dir", default="/app/data/reconciliation", help="Output directory for reconciliation report")

    args = parser.parse_args()

    if args.command != "import-r5-snapshots":
        parser.print_help()
        return

    # Parse path prefix map
    path_prefix_map = {}
    for mapping in args.path_prefix_map.split(","):
        if ":" in mapping:
            old, new = mapping.split(":", 1)
            path_prefix_map[old] = new

    # Discover chunks
    logger.info(f"Discovering DONE chunks from {args.jobs_db}")
    chunks = discover_done_chunks(args.jobs_db, path_prefix_map)
    logger.info(f"Found {len(chunks)} DONE chunks")

    # Dry run
    report = dry_run(chunks)
    logger.info(f"Import candidates: {report['import_candidate_instruments']} instruments, "
                f"{report['import_candidate_rows']} rows")
    logger.info(f"Invalid: {report['invalid_results']}, Missing: {report['missing_results']}, "
                f"Identity errors: {report['identity_errors']}")

    if args.dry_run:
        print(json.dumps(report, indent=2, default=str))
        return

    # Bootstrap store (bootstrap() may close conn, so re-create)
    from astock_api.dataset_store import DatasetStore
    store = DatasetStore(args.store_db)
    try:
        store.bootstrap()
    except Exception:
        pass  # tables may already exist
    store = DatasetStore(args.store_db)  # fresh connection after bootstrap

    # Stats before import
    stats_before = get_datasetstore_stats(store)
    logger.info(f"DatasetStore before: {stats_before['distinct_instruments']} instruments, "
                f"{stats_before['total_rows']} rows")

    # Execute import
    import_report = execute_import(store, report["import_candidates"])
    logger.info(f"Imported: {import_report['imported_instruments']} instruments, "
                f"{import_report['imported_rows']} rows")

    # Stats after import
    stats_after = get_datasetstore_stats(store)
    logger.info(f"DatasetStore after: {stats_after['distinct_instruments']} instruments, "
                f"{stats_after['total_rows']} rows")

    # Write reconciliation report
    out_path = Path(args.output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    reconciliation_report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_phase": "R5-C4A",
        "r5_done_chunks": len(chunks),
        "results_found": report["import_candidate_instruments"],
        "invalid_results": report["invalid_results"],
        "missing_results": report["missing_results"],
        "identity_errors": report["identity_errors"],
        "empty_results": report["empty_results"],
        "datasetstore_before": stats_before,
        "imported_instruments": import_report["imported_instruments"],
        "imported_rows": import_report["imported_rows"],
        "datasetstore_after": stats_after,
    }

    report_file = out_path / "r6_r5_snapshot_import.json"
    with open(report_file, "w") as f:
        json.dump(reconciliation_report, f, indent=2)

    logger.info(f"Reconciliation report written to {report_file}")
    print(json.dumps(reconciliation_report, indent=2))


if __name__ == "__main__":
    main()
