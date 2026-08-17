"""Job Engine: SQLite-backed background job system with atomic writes.

Single worker thread, crash-recovery, retry with backoff, pause/resume/cancel.
No Redis/Celery/RabbitMQ. Pure SQLite + threading.

IMPORTANT: Worker is NOT started at import time.
It must be explicitly started via JobEngine.start() from FastAPI lifespan.

r2 fixes:
- State machine transitions (no unconditional UPDATE)
- WAITING_SOURCE stops pending chunk claim, fairness for other jobs
- Worker uses conditional UPDATE with rowcount check (no race)
- recompute never overrides terminal states
- Crash recovery preserves result_path
- BEGIN IMMEDIATE for claim transaction
- Symbol ".." rejection
"""
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────
JOB_TYPES = {"market_bars_snapshot", "security_master_snapshot", "market_bars_sync"}

VALID_FREQUENCIES = {"daily", "1min", "5min", "15min", "30min", "1hour"}
SUPPORTED_FREQUENCIES = {"daily"}  # Only daily for now

SYMBOL_PATTERN = re.compile(r'^[A-Za-z0-9.\-_]+$')

# Job statuses
JOB_PENDING = "PENDING"
JOB_RUNNING = "RUNNING"
JOB_WAITING_SOURCE = "WAITING_SOURCE"
JOB_PAUSED = "PAUSED"
JOB_DONE = "DONE"
JOB_FAILED = "FAILED"
JOB_CANCELLED = "CANCELLED"

# Chunk statuses
CHUNK_PENDING = "PENDING"
CHUNK_RUNNING = "RUNNING"
CHUNK_RETRY = "RETRY"
CHUNK_DONE = "DONE"
CHUNK_FAILED = "FAILED"

# Retry policy
MAX_RETRIES = 5
RETRY_BACKOFFS = [5, 15, 45, 120, 300]  # seconds

# Terminal states
TERMINAL_STATES = {JOB_DONE, JOB_FAILED, JOB_CANCELLED}

# Allowed transitions
PAUSE_FROM = {JOB_PENDING, JOB_RUNNING, JOB_WAITING_SOURCE}
RESUME_FROM = {JOB_PAUSED}
CANCEL_FROM = {JOB_PENDING, JOB_RUNNING, JOB_WAITING_SOURCE, JOB_PAUSED}


class TransientJobError(Exception):
    """Transient error — retry with backoff."""
    pass


class PermanentJobError(Exception):
    """Permanent error — do not retry."""
    pass


class InvalidJobTransition(ValueError):
    """Invalid state transition."""
    pass


class JobEngine:
    """SQLite-backed job engine with single background worker thread."""

    def __init__(self, db_path: str, data_dir: str):
        self.db_path = db_path
        self.data_dir = data_dir
        self._stop_event = threading.Event()
        self._worker_thread = None
        self._started = False

    def _get_conn(self):
        """Get a new SQLite connection with proper pragmas.

        isolation_level=None means autocommit mode — we manage transactions
        manually with BEGIN IMMEDIATE / COMMIT / ROLLBACK.
        """
        conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def initialize(self):
        """Create tables if not exists. Safe to call multiple times."""
        conn = self._get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    params_json TEXT NOT NULL,
                    total_chunks INTEGER NOT NULL DEFAULT 0,
                    completed_chunks INTEGER NOT NULL DEFAULT 0,
                    failed_chunks INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS job_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    chunk_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    result_path TEXT,
                    last_error TEXT,
                    last_server TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id),
                    UNIQUE(job_id, chunk_key)
                )
            """)
            conn.execute("PRAGMA user_version=1")
            conn.commit()
        finally:
            conn.close()

    def _now_iso(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def _result_dir(self, job_id):
        d = os.path.join(self.data_dir, "jobs", job_id, "chunks")
        os.makedirs(d, exist_ok=True)
        return d

    def _result_path(self, job_id, chunk_key):
        h = hashlib.sha256(chunk_key.encode()).hexdigest()[:24]
        return os.path.join(self._result_dir(job_id), f"{h}.json")

    def _atomic_write_result(self, job_id, chunk_key, data):
        """Write result atomically: temp file → fsync → rename."""
        final_path = self._result_path(job_id, chunk_key)
        temp_path = final_path + ".tmp"

        result = {
            "job_id": job_id,
            "chunk_key": chunk_key,
            "saved_at": self._now_iso(),
            "data": data,
        }

        with open(temp_path, 'w') as f:
            json.dump(result, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_path, final_path)
        return final_path

    def _result_exists_and_valid(self, job_id, chunk_key):
        """Check if result file exists and is valid JSON."""
        path = self._result_path(job_id, chunk_key)
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            return data
        except (json.JSONDecodeError, OSError):
            return None

    def create_job(self, job_type: str, params: dict) -> dict:
        """Create a job with all chunks in one transaction."""
        if job_type not in JOB_TYPES:
            raise ValueError(f"Unknown job type: {job_type}")

        if job_type == "market_bars_snapshot":
            symbols = params.get("symbols", [])
            frequency = params.get("frequency", "daily")
            count = params.get("count", 100)

            # Validate frequency — only daily supported
            if frequency not in SUPPORTED_FREQUENCIES:
                raise ValueError("market_bars_snapshot currently supports daily only")

            if not symbols or len(symbols) > 500:
                raise ValueError("symbols must be 1-500")
            if not (1 <= count <= 800):
                raise ValueError("count must be 1-800")

            # Deduplicate preserving order, reject ".."
            seen = set()
            unique_symbols = []
            for s in symbols:
                s = str(s)
                if ".." in s:
                    raise ValueError(f"Invalid symbol (contains ..): {s}")
                if not SYMBOL_PATTERN.match(s):
                    raise ValueError(f"Invalid symbol: {s}")
                if s not in seen:
                    seen.add(s)
                    unique_symbols.append(s)

            job_id = str(uuid.uuid4())
            now = self._now_iso()

            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT INTO jobs (job_id, job_type, status, params_json, total_chunks, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (job_id, job_type, JOB_PENDING, json.dumps(params), len(unique_symbols), now, now)
                )

                for symbol in unique_symbols:
                    chunk_key = f"market_bars|{symbol}|{frequency}|{count}"
                    payload = {"job_type": "market_bars_snapshot", "symbol": symbol, "frequency": frequency, "count": count}
                    chunk_id = str(uuid.uuid4())
                    conn.execute(
                        "INSERT INTO job_chunks (chunk_id, job_id, chunk_key, payload_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (chunk_id, job_id, chunk_key, json.dumps(payload), CHUNK_PENDING, now, now)
                    )

                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        elif job_type == "security_master_snapshot":
            source = params.get("source", "")
            as_of = params.get("as_of", "")

            if not source:
                raise ValueError("security_master_snapshot requires 'source' param")

            job_id = str(uuid.uuid4())
            now = self._now_iso()

            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT INTO jobs (job_id, job_type, status, params_json, total_chunks, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (job_id, job_type, JOB_PENDING, json.dumps(params), 1, now, now)
                )

                chunk_key = f"security_master|{source}|{as_of}"
                payload = {"job_type": "security_master_snapshot", "job_id": job_id, "source": source, "as_of": as_of}
                chunk_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO job_chunks (chunk_id, job_id, chunk_key, payload_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (chunk_id, job_id, chunk_key, json.dumps(payload), CHUNK_PENDING, now, now)
                )

                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        elif job_type == "market_bars_sync":
            symbols = params.get("symbols", [])
            frequency = params.get("frequency", "daily")
            count = params.get("count", 100)

            # Validate frequency — only daily supported
            if frequency not in SUPPORTED_FREQUENCIES:
                raise ValueError("market_bars_sync currently supports daily only")

            if not symbols or len(symbols) > 500:
                raise ValueError("symbols must be 1-500")
            if not (1 <= count <= 800):
                raise ValueError("count must be 1-800")

            # Deduplicate preserving order, reject ".."
            seen = set()
            unique_symbols = []
            for s in symbols:
                s = str(s)
                if ".." in s:
                    raise ValueError(f"Invalid symbol (contains ..): {s}")
                # market_bars_sync requires 6-digit numeric symbols (handler contract)
                if len(s) != 6 or not s.isdigit():
                    raise ValueError(f"market_bars_sync requires 6-digit numeric symbol: {s}")
                if not SYMBOL_PATTERN.match(s):
                    raise ValueError(f"Invalid symbol: {s}")
                if s not in seen:
                    seen.add(s)
                    unique_symbols.append(s)

            job_id = str(uuid.uuid4())
            now = self._now_iso()

            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT INTO jobs (job_id, job_type, status, params_json, total_chunks, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (job_id, job_type, JOB_PENDING, json.dumps(params), len(unique_symbols), now, now)
                )

                for symbol in unique_symbols:
                    chunk_key = f"market_bars_sync|{symbol}|{frequency}|{count}"
                    payload = {"job_type": "market_bars_sync", "job_id": job_id, "symbol": symbol, "frequency": frequency, "count": count}
                    chunk_id = str(uuid.uuid4())
                    conn.execute(
                        "INSERT INTO job_chunks (chunk_id, job_id, chunk_key, payload_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (chunk_id, job_id, chunk_key, json.dumps(payload), CHUNK_PENDING, now, now)
                    )

                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        return {"job_id": job_id, "status": JOB_PENDING}

    def get_job(self, job_id: str) -> dict | None:
        conn = self._get_conn()
        try:
            cur = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            result = dict(zip(cols, row))
            if "params_json" in result:
                result["params"] = json.loads(result.pop("params_json"))
            return result
        finally:
            conn.close()

    def get_chunks(self, job_id: str, status=None, limit=100):
        conn = self._get_conn()
        try:
            query = "SELECT * FROM job_chunks WHERE job_id=?"
            params = [job_id]
            if status:
                query += " AND status=?"
                params.append(status)
            query += f" ORDER BY created_at DESC LIMIT {min(limit, 500)}"
            cur = conn.execute(query, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]
        finally:
            conn.close()

    def list_jobs(self, status=None, limit=50):
        conn = self._get_conn()
        try:
            query = "SELECT * FROM jobs WHERE 1=1"
            params = []
            if status:
                query += " AND status=?"
                params.append(status)
            query += f" ORDER BY created_at DESC LIMIT {min(limit, 200)}"
            cur = conn.execute(query, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]
        finally:
            conn.close()

    def pause_job(self, job_id: str):
        """Pause a job with state machine validation."""
        conn = self._get_conn()
        try:
            job = conn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not job:
                raise InvalidJobTransition("Job not found")
            current = job[0]
            if current in TERMINAL_STATES:
                raise InvalidJobTransition(f"Cannot pause job in {current} state")
            if current not in PAUSE_FROM:
                raise InvalidJobTransition(f"Cannot pause job in {current} state")

            now = self._now_iso()
            conn.execute("UPDATE jobs SET status=?, updated_at=? WHERE job_id=?", (JOB_PAUSED, now, job_id))
            conn.commit()
        finally:
            conn.close()

    def resume_job(self, job_id: str):
        """Resume a paused job with state machine validation."""
        conn = self._get_conn()
        try:
            job = conn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not job:
                raise InvalidJobTransition("Job not found")
            current = job[0]
            if current in TERMINAL_STATES:
                raise InvalidJobTransition(f"Cannot resume job in {current} state")
            if current not in RESUME_FROM:
                raise InvalidJobTransition(f"Cannot resume job in {current} state")

            now = self._now_iso()
            conn.execute("UPDATE jobs SET status=?, updated_at=? WHERE job_id=?", (JOB_PENDING, now, job_id))
            conn.commit()
        finally:
            conn.close()

    def cancel_job(self, job_id: str):
        """Cancel a job with state machine validation."""
        conn = self._get_conn()
        try:
            job = conn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not job:
                raise InvalidJobTransition("Job not found")
            current = job[0]
            if current in TERMINAL_STATES:
                raise InvalidJobTransition(f"Cannot cancel job in {current} state")
            if current not in CANCEL_FROM:
                raise InvalidJobTransition(f"Cannot cancel job in {current} state")

            now = self._now_iso()
            conn.execute("UPDATE jobs SET status=?, updated_at=? WHERE job_id=?", (JOB_CANCELLED, now, job_id))
            conn.commit()
        finally:
            conn.close()

    def _recompute_job_counters_in_conn(self, conn, job_id):
        """Recompute counters using existing connection. Never overrides terminal states."""
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM job_chunks WHERE job_id=? GROUP BY status", (job_id,)
        ).fetchall()
        counts = {r[0]: r[1] for r in rows}

        total = sum(counts.values())
        done = counts.get(CHUNK_DONE, 0)
        failed = counts.get(CHUNK_FAILED, 0)

        # Get current job status — never override terminal states
        cur_job = conn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        current_status = cur_job[0] if cur_job else JOB_PENDING

        # Only auto-transition from non-terminal states
        can_transition = current_status in (JOB_PENDING, JOB_RUNNING, JOB_WAITING_SOURCE)

        if can_transition:
            pending_or_retry = counts.get(CHUNK_PENDING, 0) + counts.get(CHUNK_RETRY, 0) + counts.get(CHUNK_RUNNING, 0)

            if done == total and total > 0:
                new_status = JOB_DONE
            elif pending_or_retry == 0 and failed > 0:
                new_status = JOB_FAILED
            else:
                new_status = None
        else:
            # Terminal or paused — never change status
            new_status = None

        now = self._now_iso()
        if new_status:
            conn.execute(
                "UPDATE jobs SET total_chunks=?, completed_chunks=?, failed_chunks=?, status=?, updated_at=? WHERE job_id=?",
                [total, done, failed, new_status, now, job_id]
            )
        else:
            conn.execute(
                "UPDATE jobs SET total_chunks=?, completed_chunks=?, failed_chunks=?, updated_at=? WHERE job_id=?",
                [total, done, failed, now, job_id]
            )

        if new_status in (JOB_DONE, JOB_FAILED):
            conn.execute(
                "UPDATE jobs SET finished_at=? WHERE job_id=?", (now, job_id)
            )

    def _recompute_job_counters(self, job_id):
        """Recompute counters (creates own connection)."""
        conn = self._get_conn()
        try:
            self._recompute_job_counters_in_conn(conn, job_id)
            conn.commit()
        finally:
            conn.close()

    def _claim_chunk(self, job_id) -> dict | None:
        """Claim one chunk atomically using BEGIN IMMEDIATE.

        r3 fix: status check happens INSIDE the transaction to prevent
        pause/cancel race between scheduler and claim.
        """
        conn = self._get_conn()
        try:
            # BEGIN IMMEDIATE FIRST — then check status inside transaction
            conn.execute("BEGIN IMMEDIATE")
            try:
                now = self._now_iso()

                # Re-read job status under lock (prevents pause/cancel race)
                job = conn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                if not job:
                    conn.rollback()
                    return None

                job_status = job[0]

                # Terminal/blocked states — never claim
                if job_status in (JOB_DONE, JOB_CANCELLED, JOB_PAUSED):
                    conn.rollback()
                    return None

                # WAITING_SOURCE: claim due RETRY chunks AND PENDING chunks.
                # PENDING chunks don't depend on baidu; only RETRY chunks that hit baidu OPEN need to wait.
                # Try RETRY first (due), then PENDING.
                if job_status == JOB_WAITING_SOURCE:
                    row = conn.execute("""
                        SELECT chunk_id FROM job_chunks
                        WHERE job_id=? AND status='RETRY' AND (next_retry_at IS NULL OR next_retry_at <= ?)
                        ORDER BY created_at ASC LIMIT 1
                    """, (job_id, now)).fetchone()

                    if not row:
                        # Fall back to PENDING chunks (they don't depend on baidu)
                        row = conn.execute("""
                            SELECT chunk_id FROM job_chunks
                            WHERE job_id=? AND status='PENDING'
                            ORDER BY created_at ASC LIMIT 1
                        """, (job_id,)).fetchone()

                    if not row:
                        conn.rollback()
                        return None

                    chunk_id = row[0]

                    # Atomic: claim chunk + transition job in same transaction
                    updated = conn.execute(
                        "UPDATE job_chunks SET status=?, started_at=?, updated_at=? WHERE chunk_id=? AND status IN ('RETRY','PENDING')",
                        (CHUNK_RUNNING, now, now, chunk_id)
                    ).rowcount

                    if updated == 0:
                        conn.rollback()
                        return None

                    # Transition WAITING_SOURCE -> RUNNING atomically
                    conn.execute(
                        "UPDATE jobs SET status=?, updated_at=? WHERE job_id=? AND status='WAITING_SOURCE'",
                        (JOB_RUNNING, now, job_id)
                    )

                else:
                    # PENDING or RUNNING: claim any PENDING or due RETRY
                    row = conn.execute("""
                        SELECT chunk_id FROM job_chunks
                        WHERE job_id=? AND (status='PENDING' OR (status='RETRY' AND (next_retry_at IS NULL OR next_retry_at <= ?)))
                        ORDER BY created_at ASC LIMIT 1
                    """, (job_id, now)).fetchone()

                    if not row:
                        conn.rollback()
                        return None

                    chunk_id = row[0]

                    updated = conn.execute(
                        "UPDATE job_chunks SET status=?, started_at=?, updated_at=? WHERE chunk_id=? AND status IN ('PENDING','RETRY')",
                        (CHUNK_RUNNING, now, now, chunk_id)
                    ).rowcount

                    if updated == 0:
                        conn.rollback()
                        return None

                # Get full chunk data
                cur = conn.execute("SELECT * FROM job_chunks WHERE chunk_id=?", (chunk_id,))
                chunk_row = cur.fetchone()
                cols = [d[0] for d in cur.description]

                conn.commit()
                return dict(zip(cols, chunk_row))
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def _execute_chunk(self, chunk: dict, handler_func):
        """Execute a single chunk with retry logic.

        Returns 'transient' if transient error occurred (caller should stop processing this job).
        """
        job_id = chunk["job_id"]
        chunk_key = chunk["chunk_key"]

        # Check if result already exists (crash recovery)
        existing = self._result_exists_and_valid(job_id, chunk_key)
        if existing is not None:
            # File exists and valid — skip network call, mark done WITH result_path
            logger.info("Chunk %s: result file exists, skipping", chunk_key)
            result_path = self._result_path(job_id, chunk_key)
            self._mark_chunk_done(chunk["chunk_id"], job_id, result_path)
            return None  # success

        payload = json.loads(chunk.get("payload_json", "{}")) if isinstance(chunk.get("payload_json"), str) else chunk.get("payload_json", {})

        try:
            data = handler_func(payload)
        except TransientJobError as e:
            retry_count = chunk.get("retry_count", 0) or 0
            if retry_count < MAX_RETRIES:
                backoff = RETRY_BACKOFFS[retry_count] if retry_count < len(RETRY_BACKOFFS) else RETRY_BACKOFFS[-1]
                next_retry = (datetime.now(timezone.utc) + timedelta(seconds=backoff)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                self._mark_chunk_retry(chunk["chunk_id"], job_id, retry_count + 1, next_retry, str(e))
            else:
                self._mark_chunk_failed(chunk["chunk_id"], job_id, str(e))
            return "transient"  # Signal caller to stop processing this job
        except PermanentJobError as e:
            self._mark_chunk_failed(chunk["chunk_id"], job_id, str(e))
            return None  # Permanent failure — continue with other chunks
        except Exception as e:
            # Unclassified exception → FAILED immediately.
            # Do NOT retry or set WAITING_SOURCE: local deterministic errors
            # (e.g., DuckDB catalog missing, file I/O) will never heal on retry.
            # Expected upstream transients must arrive as TransientJobError.
            logger.error("Chunk %s: unclassified exception — marking FAILED", chunk_key, exc_info=True)
            self._mark_chunk_failed(chunk["chunk_id"], job_id, str(e))
            return None  # Failure — continue with other chunks

        # Atomic write: file first, then DB
        result_path = self._atomic_write_result(job_id, chunk_key, data)
        self._mark_chunk_done(chunk["chunk_id"], job_id, result_path)
        return None  # success

    def _mark_chunk_done(self, chunk_id: str, job_id: str, result_path: str | None):
        conn = self._get_conn()
        try:
            now = self._now_iso()
            conn.execute(
                "UPDATE job_chunks SET status=?, result_path=?, finished_at=?, updated_at=? WHERE chunk_id=?",
                (CHUNK_DONE, result_path, now, now, chunk_id)
            )
            self._recompute_job_counters_in_conn(conn, job_id)
            conn.commit()
        finally:
            conn.close()

    def _mark_chunk_retry(self, chunk_id: str, job_id: str, retry_count: int, next_retry_at: str, error: str):
        conn = self._get_conn()
        try:
            now = self._now_iso()
            conn.execute(
                "UPDATE job_chunks SET status=?, retry_count=?, next_retry_at=?, last_error=?, updated_at=? WHERE chunk_id=?",
                (CHUNK_RETRY, retry_count, next_retry_at, error, now, chunk_id)
            )

            # Job goes to WAITING_SOURCE (conditional — don't override terminal states)
            job = conn.execute(
                "SELECT status FROM jobs WHERE job_id=? AND status NOT IN ('DONE','CANCELLED','PAUSED')", (job_id,)
            ).fetchone()
            if job and job[0] not in (JOB_WAITING_SOURCE,):
                conn.execute(
                    "UPDATE jobs SET status=?, last_error=?, updated_at=? WHERE job_id=?",
                    (JOB_WAITING_SOURCE, error, now, job_id)
                )

            conn.commit()
        finally:
            conn.close()

    def _mark_chunk_failed(self, chunk_id: str, job_id: str, error: str):
        conn = self._get_conn()
        try:
            now = self._now_iso()
            conn.execute(
                "UPDATE job_chunks SET status=?, last_error=?, finished_at=?, updated_at=? WHERE chunk_id=?",
                (CHUNK_FAILED, error, now, now, chunk_id)
            )
            self._recompute_job_counters_in_conn(conn, job_id)
            conn.commit()
        finally:
            conn.close()

    def _recover_state(self):
        """Recover state after crash: RUNNING chunks → RETRY, RUNNING jobs → PENDING."""
        conn = self._get_conn()
        try:
            now = self._now_iso()

            # RUNNING chunks → RETRY (but will skip if result file exists)
            conn.execute(
                "UPDATE job_chunks SET status=?, next_retry_at=?, updated_at=? WHERE status=?",
                (CHUNK_RETRY, now, now, CHUNK_RUNNING)
            )

            # RUNNING jobs → PENDING (WAITING_SOURCE stays; PAUSED/CANCELLED/DONE/FAILED stay)
            conn.execute(
                "UPDATE jobs SET status=? WHERE status=?", (JOB_PENDING, JOB_RUNNING)
            )

            # Recompute all job counters in same connection
            jobs = conn.execute("SELECT job_id FROM jobs").fetchall()
            for (jid,) in jobs:
                self._recompute_job_counters_in_conn(conn, jid)

            conn.commit()
        finally:
            conn.close()

    def start(self, handler_func):
        """Start the worker thread. Must be called from lifespan."""
        if self._started:
            return

        # Recover state on startup
        self._recover_state()

        self._started = True
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop, args=(handler_func,), daemon=True
        )
        self._worker_thread.start()
        logger.info("Job engine worker started")

    def stop(self):
        """Signal the worker to stop."""
        self._stop_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=30)
        logger.info("Job engine worker stopped")

    def _is_job_runnable(self, job_id):
        """Check if a job is runnable (has chunks that can execute right now).

        r4 fix: PENDING/RUNNING must also verify there are actually executable
        chunks (not just future RETRY). Prevents fairness bug where a job with
        only future retries blocks other jobs.
        """
        conn = self._get_conn()
        try:
            job = conn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not job:
                return False

            status = job[0]
            if status in (JOB_DONE, JOB_CANCELLED, JOB_PAUSED):
                return False

            now = self._now_iso()

            if status == JOB_WAITING_SOURCE:
                # Only runnable if RETRY chunk is due now
                retry_due = conn.execute("""
                    SELECT COUNT(*) FROM job_chunks
                    WHERE job_id=? AND status='RETRY' AND (next_retry_at IS NULL OR next_retry_at <= ?)
                """, (job_id, now)).fetchone()[0]
                return retry_due > 0

            # PENDING or RUNNING: only runnable if there are executable chunks
            # (PENDING chunk OR due RETRY chunk)
            runnable_chunks = conn.execute("""
                SELECT COUNT(*) FROM job_chunks
                WHERE job_id=? AND (status='PENDING' OR (status='RETRY' AND (next_retry_at IS NULL OR next_retry_at <= ?)))
            """, (job_id, now)).fetchone()[0]
            return runnable_chunks > 0
        finally:
            conn.close()

    def _try_transition_to_running(self, job_id):
        """Try to transition a job to RUNNING. Returns True if allowed to proceed.

        r4 fix: WAITING_SOURCE does NOT transition here — only _claim_chunk()
        transitions it atomically (same transaction as chunk claim). This prevents
        the race where scheduler sets WAITING_SOURCE->RUNNING, then _claim_chunk
        sees RUNNING and claims PENDING instead of due RETRY.
        """
        conn = self._get_conn()
        try:
            now = self._now_iso()

            # Get current status under lock
            job = conn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not job:
                return False

            current = job[0]

            # Conditional transition — only from allowed states
            if current == JOB_PENDING:
                updated = conn.execute(
                    "UPDATE jobs SET status=?, updated_at=? WHERE job_id=? AND status='PENDING'",
                    (JOB_RUNNING, now, job_id)
                ).rowcount
            elif current == JOB_WAITING_SOURCE:
                # r4: Do NOT transition WAITING_SOURCE here.
                # _claim_chunk() will do it atomically in the same transaction as chunk claim.
                return True  # Allow proceeding to _claim_chunk
            else:
                # PAUSED, CANCELLED, DONE, FAILED — don't override
                return False

            conn.commit()
            return updated > 0
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return False
        finally:
            conn.close()

    def _worker_loop(self, handler_func):
        """Main worker loop."""
        while not self._stop_event.is_set():
            # Find a runnable job (skip WAITING_SOURCE unless retry is due)
            conn = self._get_conn()
            try:
                job_rows = conn.execute("""
                    SELECT job_id, job_type FROM jobs
                    WHERE status IN ('PENDING', 'RUNNING', 'WAITING_SOURCE')
                    ORDER BY created_at ASC
                """).fetchall()
            finally:
                conn.close()

            found_runnable = False
            for job_row in job_rows:
                job_id, job_type = job_row

                if not self._is_job_runnable(job_id):
                    continue

                # Try conditional transition to RUNNING
                if not self._try_transition_to_running(job_id):
                    continue  # State changed (pause/cancel), skip

                found_runnable = True

                # Process chunks one by one
                while not self._stop_event.is_set():
                    chunk = self._claim_chunk(job_id)
                    if not chunk:
                        # No more chunks — check job status
                        self._recompute_job_counters(job_id)

                        # Check if job is done/failed/cancelled
                        conn = self._get_conn()
                        try:
                            job = conn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                            if job and job[0] in (JOB_DONE, JOB_FAILED, JOB_CANCELLED):
                                break
                            if job and job[0] == JOB_WAITING_SOURCE:
                                # Wait for retry time, then try next job
                                break
                            if job and job[0] == JOB_PAUSED:
                                break
                        finally:
                            conn.close()

                        # Wait before checking again
                        self._stop_event.wait(1.0)
                        continue

                    # Execute chunk handler
                    result = self._execute_chunk(chunk, handler_func)

                    if result == "transient":
                        # Transient error — job is now WAITING_SOURCE
                        # Stop processing this job, return to outer scheduler
                        break

                    # Minimum 1 second between chunks
                    self._stop_event.wait(1.0)

            # Sleep after ALL jobs checked — not inside the for loop
            if not found_runnable:
                self._stop_event.wait(1.0)