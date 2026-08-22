"""Scheduler State: persistent memory for scheduler runs.

Tracks each update cycle's lifecycle (RUNNING → SUCCESS/FAILED/INTERRUPTED).
Uses the existing JobEngine SQLite database — no new DB, no schema migration
beyond CREATE IF NOT EXISTS.

Recovery: stale RUNNING records are marked INTERRUPTED on initialization.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def initialize_scheduler_state(engine) -> None:
    """Create scheduler_runs table if it doesn't exist and recover stale RUNNING records.

    Args:
        engine: JobEngine instance with _get_conn().
    """
    conn = engine._get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduler_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_name TEXT DEFAULT 'update_cycle',
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL DEFAULT 'RUNNING',
                plan_hash TEXT,
                created_jobs INTEGER DEFAULT 0,
                error_message TEXT
            )
        """)

        # R6-7C: Recover stale RUNNING records → INTERRUPTED
        cur = conn.execute(
            "SELECT id FROM scheduler_runs WHERE status='RUNNING'"
        )
        stale_ids = [row[0] for row in cur.fetchall()]
        if stale_ids:
            placeholders = ",".join("?" * len(stale_ids))
            conn.execute(
                f"UPDATE scheduler_runs SET status='INTERRUPTED', finished_at=? WHERE id IN ({placeholders})",
                [datetime.now(timezone.utc).isoformat()] + stale_ids,
            )
            logger.info(f"Recovered {len(stale_ids)} stale scheduler run(s) → INTERRUPTED")

        conn.commit()
    finally:
        conn.close()


def start_run(engine, task_name: str = "update_cycle") -> int:
    """Create a new scheduler run record with RUNNING status.

    Args:
        engine: JobEngine instance.
        task_name: Logical task identifier (default: update_cycle).

    Returns:
        The new run id.
    """
    conn = engine._get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO scheduler_runs (task_name, started_at, status) VALUES (?, ?, 'RUNNING')",
            (task_name, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def complete_run(engine, run_id: int, plan_hash: str | None = None, created_jobs: int = 0) -> None:
    """Mark a scheduler run as SUCCESS.

    Args:
        engine: JobEngine instance.
        run_id: The run id returned by start_run().
        plan_hash: Optional plan hash for provenance.
        created_jobs: Number of jobs created in this cycle.
    """
    conn = engine._get_conn()
    try:
        conn.execute(
            "UPDATE scheduler_runs SET status='SUCCESS', finished_at=?, plan_hash=?, created_jobs=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), plan_hash, created_jobs, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def fail_run(engine, run_id: int, error_message: str) -> None:
    """Mark a scheduler run as FAILED.

    Args:
        engine: JobEngine instance.
        run_id: The run id returned by start_run().
        error_message: Error description.
    """
    conn = engine._get_conn()
    try:
        conn.execute(
            "UPDATE scheduler_runs SET status='FAILED', finished_at=?, error_message=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), error_message, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_latest_run(engine) -> dict | None:
    """Get the most recent scheduler run.

    Args:
        engine: JobEngine instance.

    Returns:
        Dict with run details, or None if no runs exist.
    """
    # Ensure table exists without triggering interrupted recovery
    conn = engine._get_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS scheduler_runs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "task_name TEXT NOT NULL,"
            "started_at TEXT NOT NULL,"
            "finished_at TEXT,"
            "status TEXT NOT NULL DEFAULT 'RUNNING',"
            "plan_hash TEXT,"
            "created_jobs INTEGER DEFAULT 0,"
            "error_message TEXT"
            ")"
        )
    finally:
        conn.close()

    conn = engine._get_conn()
    try:
        cur = conn.execute(
            "SELECT id, task_name, started_at, finished_at, status, plan_hash, created_jobs, error_message "
            "FROM scheduler_runs ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    finally:
        conn.close()


def get_last_successful_run(engine) -> dict | None:
    """Get the most recent SUCCESS run.

    Args:
        engine: JobEngine instance.

    Returns:
        Dict with run details, or None if no successful runs exist.
    """
    conn = engine._get_conn()
    try:
        cur = conn.execute(
            "SELECT id, task_name, started_at, finished_at, status, plan_hash, created_jobs, error_message "
            "FROM scheduler_runs WHERE status='SUCCESS' ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    finally:
        conn.close()
