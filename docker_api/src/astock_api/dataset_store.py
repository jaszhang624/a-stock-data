"""Dataset Store: DuckDB-backed canonical data plane.

Separate from astock_jobs.db (control plane).
Path: /app/data/astock_data.duckdb

Tables:
- security_master_snapshots (versioned snapshot metadata)
- security_master (canonical security records, versioned by snapshot_id)
- market_bars_daily (canonical daily bars, PK = security_id + trade_date)
- dataset_heads (active snapshot pointers)
- ingestion_log (audit trail)

Crash safety: DuckDB ACID transactions. Single-writer model.
"""
import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class DatasetStore:
    """DuckDB canonical data plane."""

    def __init__(self, duckdb_path: str):
        """Initialize dataset store.

        Args:
            duckdb_path: Absolute path to DuckDB file (e.g., /app/data/astock_data.duckdb)
                         Use ':memory:' for in-memory testing.
        """
        # Allow :memory: for tests, or validate path doesn't escape data directory
        if duckdb_path != ':memory:':
            real_path = os.path.realpath(duckdb_path)
            if not real_path.startswith("/app/data"):
                raise ValueError(f"Dataset path must be under /app/data: {duckdb_path}")

        self.duckdb_path = duckdb_path
        self._conn = None

    def get_conn(self):
        """Get or create DuckDB connection (lazy init)."""
        if self._conn is None:
            import duckdb
            self._conn = duckdb.connect(self.duckdb_path)
        return self._conn

    def bootstrap(self):
        """Create all tables if not exists. Idempotent."""
        conn = self.get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS security_master_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                as_of TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                checksum TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'STAGING',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS security_master (
                snapshot_id TEXT NOT NULL,
                security_id TEXT NOT NULL,
                code TEXT NOT NULL,
                exchange TEXT NOT NULL,
                security_type TEXT NOT NULL,
                board TEXT,
                name TEXT NOT NULL,
                listing_status TEXT DEFAULT 'active',
                list_date TEXT,
                delist_date TEXT,
                source TEXT NOT NULL,
                as_of TEXT NOT NULL,
                FOREIGN KEY (snapshot_id) REFERENCES security_master_snapshots(snapshot_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_bars_daily (
                security_id TEXT NOT NULL,
                trade_date DATE NOT NULL,
                open DOUBLE,
                high DOUBLE,
                low DOUBLE,
                close DOUBLE,
                volume BIGINT NOT NULL DEFAULT 0,
                amount DOUBLE NOT NULL DEFAULT 0,
                source TEXT NOT NULL,
                ingested_at TEXT NOT NULL,
                job_id TEXT,
                PRIMARY KEY (security_id, trade_date)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dataset_heads (
                dataset_name TEXT PRIMARY KEY,
                active_snapshot_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ingestion_log (
                log_id TEXT PRIMARY KEY,
                job_type TEXT NOT NULL,
                job_id TEXT NOT NULL,
                table_name TEXT NOT NULL,
                rows_inserted INTEGER NOT NULL DEFAULT 0,
                rows_updated INTEGER NOT NULL DEFAULT 0,
                rows_unchanged INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                status TEXT NOT NULL
            )
        """)
        conn.commit()

    @staticmethod
    def _now_iso():
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    @staticmethod
    def code_to_exchange(code: str) -> str:
        """Map 6-digit code to exchange using equity prefix rules.

        SSE: codes starting with '6' or '9' (Shanghai)
        SZSE: codes starting with '0' or '3' (Shenzhen)
        BSE: codes starting with '4', '8', or '92' (Beijing)

        WARNING: This method uses EQUITY-only prefix inference.
        For INDEX instruments, use make_security_id(code, exchange=...) with explicit exchange.
        """
        if code.startswith(('6', '9')):
            return 'SSE'
        elif code.startswith(('0', '3')):
            return 'SZSE'
        elif code.startswith(('4', '8', '92')):
            return 'BSE'
        else:
            raise ValueError(f"Unknown exchange for code: {code}")

    @staticmethod
    def make_security_id(code: str, exchange: str | None = None) -> str:
        """Create canonical security_id from code.

        Format: "<exchange>:<code>" e.g., "SSE:600519"

        Args:
            code: 6-digit numeric string (e.g., "600519")
            exchange: Optional explicit exchange. If provided, bypasses prefix inference.
                      Required for INDEX instruments where prefix does not determine exchange.

        Examples:
            >>> make_security_id("600519")  # equity prefix inference
            'SSE:600519'

            >>> make_security_id("000001", exchange="SZSE")  # explicit equity
            'SZSE:000001'

            >>> make_security_id("000001", exchange="SSE")  # INDEX on SSE
            'SSE:000001'

        Raises:
            ValueError: code is invalid, exchange inference fails, or explicit exchange is not recognized.
        """
        if exchange is not None:
            if exchange.upper() not in {"SSE", "SZSE", "BSE"}:
                raise ValueError(f"Invalid exchange '{exchange}', must be one of SSE, SZSE, BSE")
            return f"{exchange.upper()}:{code}"
        inferred = DatasetStore.code_to_exchange(code)
        return f"{inferred}:{code}"

    def write_market_bars(self, security_id: str, bars: list[dict], source: str, job_id: str):
        """Idempotent UPSERT of daily bars into market_bars_daily.

        Args:
            security_id: Canonical ID (e.g., "SSE:600519")
            bars: List of dicts with keys: trade_date, open, high, low, close, volume, amount
            source: Upstream provenance ('mootdx' / 'baidu')
            job_id: Execution artifact reference

        Returns:
            dict with rows_inserted, rows_updated counts (approximate)
        """
        conn = self.get_conn()
        now = self._now_iso()

        for bar in bars:
            trade_date = str(bar.get('trade_date', ''))[:10]  # YYYY-MM-DD
            conn.execute("""
                INSERT INTO market_bars_daily 
                    (security_id, trade_date, open, high, low, close, volume, amount, source, ingested_at, job_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (security_id, trade_date) 
                DO UPDATE SET 
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume, amount=excluded.amount,
                    ingested_at=excluded.ingested_at
            """, [security_id, trade_date, bar.get('open'), bar.get('high'), bar.get('low'),
                  bar.get('close'), int(bar.get('volume', 0)), float(bar.get('amount', 0)),
                  source, now, job_id])

        conn.commit()
        return {"rows_inserted": len(bars), "rows_updated": 0}

    def write_security_master_snapshot(self, snapshot_id: str, securities: list[dict]):
        """Write security master rows for a given snapshot_id.

        Args:
            snapshot_id: UUID of the staging snapshot
            securities: List of dicts with code, exchange, security_type, board, name, etc.

        Returns:
            row_count written
        """
        conn = self.get_conn()
        now = self._now_iso()

        for sec in securities:
            code = str(sec.get('code', ''))[:6]
            exchange = sec.get('exchange', '')
            security_id = f"{exchange}:{code}"

            conn.execute("""
                INSERT INTO security_master 
                    (snapshot_id, security_id, code, exchange, security_type, board, name, listing_status, list_date, delist_date, source, as_of)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [snapshot_id, security_id, code, exchange,
                  sec.get('security_type', 'stock'), sec.get('board'), sec.get('name', ''),
                  sec.get('listing_status', 'active'), sec.get('list_date'), sec.get('delist_date'),
                  sec.get('source', ''), sec.get('as_of', '')])

        conn.commit()
        return len(securities)

    def create_snapshot(self, source: str, as_of: str, row_count: int, checksum: str, status: str = 'STAGING'):
        """Create a snapshot metadata record.

        Returns:
            snapshot_id (UUID string)
        """
        conn = self.get_conn()
        snapshot_id = str(uuid.uuid4())
        now = self._now_iso()

        conn.execute("""
            INSERT INTO security_master_snapshots (snapshot_id, source, as_of, row_count, checksum, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [snapshot_id, source, as_of, row_count, checksum, status, now])
        conn.commit()
        return snapshot_id

    def validate_snapshot(self, snapshot_id: str) -> dict:
        """Run quality gates on a STAGING snapshot.

        Returns validation result dict with gate results and overall pass/fail.
        """
        conn = self.get_conn()

        # Get snapshot info
        snap = conn.execute(
            "SELECT * FROM security_master_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()

        if not snap:
            return {"valid": False, "error": f"Snapshot {snapshot_id} not found"}

        if snap[5] != 'STAGING':  # status field
            return {"valid": False, "error": f"Snapshot {snapshot_id} is not STAGING (status={snap[5]})"}

        # Use actual row count from security_master table, not metadata
        actual_row_count = conn.execute(
            "SELECT COUNT(*) FROM security_master WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()[0]

        # Gate 1: Unique (exchange, code)
        unique_count = conn.execute(
            "SELECT COUNT(DISTINCT exchange || ':' || code) FROM security_master WHERE snapshot_id=?",
            (snapshot_id,)
        ).fetchone()[0]
        duplicate_count = actual_row_count - unique_count

        # Gate 2: Total coverage (>=4500)
        total_coverage_pass = actual_row_count >= 4500

        # Gate 3: SSE coverage (>=1800)
        sse_count = conn.execute(
            "SELECT COUNT(*) FROM security_master WHERE snapshot_id=? AND exchange='SSE'", (snapshot_id,)
        ).fetchone()[0]

        # Gate 4: SZSE coverage (>=2500)
        szse_count = conn.execute(
            "SELECT COUNT(*) FROM security_master WHERE snapshot_id=? AND exchange='SZSE'", (snapshot_id,)
        ).fetchone()[0]

        # Gate 5: BSE coverage (>=200)
        bse_count = conn.execute(
            "SELECT COUNT(*) FROM security_master WHERE snapshot_id=? AND exchange='BSE'", (snapshot_id,)
        ).fetchone()[0]

        # Gate 6: Security type distribution
        type_dist = conn.execute(
            "SELECT security_type, COUNT(*) FROM security_master WHERE snapshot_id=? GROUP BY security_type",
            (snapshot_id,)
        ).fetchall()

        # Gate 7: Duplicate rate = 0
        duplicate_rate = duplicate_count / max(actual_row_count, 1)

        # Gate 8: Delta vs previous active snapshot
        prev_active = conn.execute("""
            SELECT s.row_count FROM security_master_snapshots s
            JOIN dataset_heads h ON s.snapshot_id = h.active_snapshot_id
            WHERE h.dataset_name = 'security_master' AND h.status = 'ACTIVE'
            ORDER BY s.created_at DESC LIMIT 1
        """).fetchone()

        delta_pct = abs(actual_row_count - prev_active[0]) / max(prev_active[0], 1) if prev_active else None
        delta_pass = delta_pct is None or delta_pct < 0.3

        # Overall validation (for VALIDATED status — BSE not required)
        all_pass = (
            duplicate_count == 0 and
            total_coverage_pass and
            sse_count >= 1800 and
            szse_count >= 2500 and
            delta_pass
        )

        # BSE coverage check (separate — only blocks ACTIVE, not VALIDATED)
        bse_pass = bse_count >= 200

        result = {
            "valid": all_pass,
            "bse_coverage_satisfied": bse_pass,
            "snapshot_id": snapshot_id,
            "gates": {
                "unique_exchange_code": {"pass": duplicate_count == 0, "value": unique_count, "total": actual_row_count},
                "total_coverage": {"pass": total_coverage_pass, "value": actual_row_count, "threshold": 4500},
                "sse_coverage": {"pass": sse_count >= 1800, "value": sse_count, "threshold": 1800},
                "szse_coverage": {"pass": szse_count >= 2500, "value": szse_count, "threshold": 2500},
                "bse_coverage": {"pass": bse_pass, "value": bse_count, "threshold": 200},
                "security_type_distribution": {"pass": len(type_dist) > 0, "value": {str(t[0]): t[1] for t in type_dist}},
                "duplicate_rate": {"pass": duplicate_count == 0, "value": duplicate_rate},
                "delta_vs_previous": {"pass": delta_pass, "value": delta_pct, "threshold": 0.3} if prev_active else {"pass": True, "value": None, "note": "no previous snapshot"},
            }
        }

        return result

    def update_snapshot_status(self, snapshot_id: str, status: str):
        """Update snapshot status (STAGING → VALIDATED/REJECTED)."""
        conn = self.get_conn()
        conn.execute(
            "UPDATE security_master_snapshots SET status=? WHERE snapshot_id=?", (status, snapshot_id)
        )
        conn.commit()

    def activate_snapshot(self, dataset_name: str, snapshot_id: str):
        """Atomic pointer switch to make a VALIDATED snapshot ACTIVE.

        Args:
            dataset_name: 'security_master' or 'market_bars_daily'
            snapshot_id: UUID of the VALIDATED snapshot to activate
        """
        conn = self.get_conn()
        now = self._now_iso()

        # Verify snapshot is VALIDATED
        snap = conn.execute(
            "SELECT status FROM security_master_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()

        if not snap:
            raise ValueError(f"Snapshot {snapshot_id} not found")
        if snap[0] != 'VALIDATED':
            raise ValueError(f"Snapshot {snapshot_id} is not VALIDATED (status={snap[0]})")

        # Atomic pointer switch
        conn.execute("""
            INSERT INTO dataset_heads (dataset_name, active_snapshot_id, status, updated_at)
            VALUES (?, ?, 'ACTIVE', ?)
            ON CONFLICT (dataset_name) 
            DO UPDATE SET active_snapshot_id=?, status='ACTIVE', updated_at=?
        """, [dataset_name, snapshot_id, now, snapshot_id, now])

        # Mark old ACTIVE as STALE (not the new one)
        conn.execute("""
            UPDATE security_master_snapshots SET status='STALE'
            WHERE snapshot_id != ? AND status = 'ACTIVE'
        """, [snapshot_id])

        # Mark new snapshot as ACTIVE
        conn.execute(
            "UPDATE security_master_snapshots SET status='ACTIVE' WHERE snapshot_id=?", (snapshot_id,)
        )

        conn.commit()

    def get_active_snapshot(self, dataset_name: str):
        """Get active snapshot_id for a dataset. Returns None if not set."""
        conn = self.get_conn()
        row = conn.execute(
            "SELECT active_snapshot_id FROM dataset_heads WHERE dataset_name=? AND status='ACTIVE'",
            (dataset_name,)
        ).fetchone()
        return row[0] if row else None

    def get_latest_trade_date(self, security_id: str) -> str | None:
        """Get the latest stored trade_date for a given security_id.

        Args:
            security_id: Canonical ID (e.g., "SSE:600519")

        Returns:
            Latest trade_date as string "YYYY-MM-DD", or None if no data exists.

        Preserves explicit Instrument/exchange identity:
        SSE:000001 INDEX queries only SSE:000001, never SZSE:000001 EQUITY.
        """
        conn = self.get_conn()
        row = conn.execute(
            "SELECT MAX(trade_date) FROM market_bars_daily WHERE security_id=?",
            (security_id,)
        ).fetchone()
        if row and row[0]:
            return str(row[0])[:10]  # YYYY-MM-DD
        return None

    def get_instrument_state(self, security_id: str) -> dict | None:
        """Get aggregate state for a single instrument.

        Returns security_id, row_count, earliest_trade_date, latest_trade_date.
        Returns None if no data exists for this security_id.

        Uses a single aggregate query — does not load bars into Python.
        """
        conn = self.get_conn()
        row = conn.execute(
            "SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM market_bars_daily WHERE security_id=?",
            (security_id,)
        ).fetchone()
        if not row or row[0] == 0:
            return None
        return {
            "security_id": security_id,
            "row_count": row[0],
            "earliest_trade_date": str(row[1])[:10] if row[1] else None,
            "latest_trade_date": str(row[2])[:10] if row[2] else None,
        }

    def get_all_instrument_states(self) -> list[dict]:
        """Get aggregate state for ALL instruments in one query.

        Returns list of dicts with security_id, row_count, earliest_trade_date, latest_trade_date.
        Uses GROUP BY — avoids N+1 database access pattern.

        Result is sorted by security_id for deterministic output.
        """
        conn = self.get_conn()
        rows = conn.execute(
            "SELECT security_id, COUNT(*) as cnt, MIN(trade_date), MAX(trade_date) "
            "FROM market_bars_daily GROUP BY security_id ORDER BY security_id"
        ).fetchall()
        return [
            {
                "security_id": row[0],
                "row_count": row[1],
                "earliest_trade_date": str(row[2])[:10] if row[2] else None,
                "latest_trade_date": str(row[3])[:10] if row[3] else None,
            }
            for row in rows
        ]

    def log_ingestion(self, job_type: str, job_id: str, table_name: str,
                      rows_inserted: int = 0, rows_updated: int = 0, rows_unchanged: int = 0,
                      status: str = 'success'):
        """Log ingestion metadata."""
        conn = self.get_conn()
        now = self._now_iso()
        log_id = str(uuid.uuid4())

        conn.execute("""
            INSERT INTO ingestion_log (log_id, job_type, job_id, table_name, rows_inserted, rows_updated, rows_unchanged, started_at, completed_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [log_id, job_type, job_id, table_name, rows_inserted, rows_updated, rows_unchanged,
              now, now, status])
        conn.commit()
