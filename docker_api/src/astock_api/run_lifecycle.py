"""Run Lifecycle: production-level orchestration record for update cycles.

Tracks the full lifecycle of each update run with granular states:
CREATED → RUNNING → PLANNED → EXECUTING → VERIFYING → SUCCESS/FAILED/INTERRUPTED

Uses the existing JobEngine SQLite database — no new DB.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _ensure_table(engine) -> None:
    """Create update_runs table if it doesn't exist (idempotent).

    Also adds the ``job_ids`` column (JSON array) if missing, so that
    run-level verification can be scoped to the exact jobs materialized
    by that run.
    """
    conn = engine._get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS update_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                reference_date TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL DEFAULT 'CREATED',
                plan_hash TEXT,
                jobs_created INTEGER DEFAULT 0,
                jobs_done INTEGER DEFAULT 0,
                jobs_failed INTEGER DEFAULT 0,
                quality_status TEXT,
                error_message TEXT,
                job_ids TEXT
            )
        """)
        conn.commit()
        # Add job_ids column to pre-existing tables (idempotent)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(update_runs)")]
        if "job_ids" not in cols:
            conn.execute("ALTER TABLE update_runs ADD COLUMN job_ids TEXT")
            conn.commit()
    finally:
        conn.close()


def initialize_run_lifecycle(engine) -> None:
    """Ensure table exists and recover stale RUNNING/PLANNED/EXECUTING/VERIFYING records.

    Args:
        engine: JobEngine instance with _get_conn().
    """
    _ensure_table(engine)

    conn = engine._get_conn()
    try:
        # Recover stale in-progress records → INTERRUPTED
        cur = conn.execute(
            "SELECT run_id FROM update_runs WHERE status IN ('RUNNING', 'PLANNED', 'EXECUTING', 'VERIFYING')"
        )
        stale_ids = [row[0] for row in cur.fetchall()]
        if stale_ids:
            placeholders = ",".join("?" * len(stale_ids))
            conn.execute(
                f"UPDATE update_runs SET status='INTERRUPTED', finished_at=? WHERE run_id IN ({placeholders})",
                [datetime.now(timezone.utc).isoformat()] + stale_ids,
            )
            logger.info(f"Recovered {len(stale_ids)} stale run(s) → INTERRUPTED")
            conn.commit()
    finally:
        conn.close()


def create_run(engine, reference_date: str) -> int:
    """Create a new run record with CREATED status.

    Args:
        engine: JobEngine instance.
        reference_date: YYYY-MM-DD date for this cycle.

    Returns:
        The new run_id.
    """
    _ensure_table(engine)

    conn = engine._get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO update_runs (reference_date, started_at, status) VALUES (?, ?, 'CREATED')",
            (reference_date, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def transition(engine, run_id: int, status: str, **kwargs) -> None:
    """Transition a run to a new state.

    Args:
        engine: JobEngine instance.
        run_id: The run id from create_run().
        status: New status (RUNNING, PLANNED, EXECUTING, VERIFYING, SUCCESS, FAILED).
        **kwargs: Additional fields to update (plan_hash, jobs_created, etc.).
    """
    conn = engine._get_conn()
    try:
        # Build SET clause dynamically
        sets = ["status=?", "finished_at=?"]
        values = [status, datetime.now(timezone.utc).isoformat() if status in ("SUCCESS", "FAILED", "INTERRUPTED") else None]

        for key, val in kwargs.items():
            if key != "run_id":
                sets.append(f"{key}=?")
                values.append(val)

        values.append(run_id)
        set_clause = ", ".join(sets)

        conn.execute(
            f"UPDATE update_runs SET {set_clause} WHERE run_id=?",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def start_run(engine, run_id: int) -> None:
    """Transition to RUNNING."""
    transition(engine, run_id, "RUNNING")


def set_planned(engine, run_id: int, plan_hash: str) -> None:
    """Transition to PLANNED after plan generation."""
    transition(engine, run_id, "PLANNED", plan_hash=plan_hash)


def set_executing(engine, run_id: int, jobs_created: int = 0, job_ids: list[str] | None = None) -> None:
    """Transition to EXECUTING after job materialization.

    Args:
        engine: JobEngine instance.
        run_id: Run id.
        jobs_created: Number of jobs created by this run.
        job_ids: Explicit list of job IDs materialized by this run.
            Stored as JSON for run-scoped verification.
    """
    import json as _json
    # Store "[]" for zero-job runs (explicit ownership: this run owns nothing)
    # Store NULL for legacy callers that don't pass job_ids.
    job_ids_json = _json.dumps(job_ids) if job_ids is not None else None
    transition(engine, run_id, "EXECUTING", jobs_created=jobs_created,
               job_ids=job_ids_json)


def set_verifying(engine, run_id: int) -> None:
    """Transition to VERIFYING for quality audit."""
    transition(engine, run_id, "VERIFYING")


def complete_run(engine, run_id: int, plan_hash: str | None = None,
                 jobs_created: int = 0, jobs_done: int = 0,
                 jobs_failed: int = 0, quality_status: str | None = None) -> None:
    """Mark run as SUCCESS."""
    transition(engine, run_id, "SUCCESS",
               plan_hash=plan_hash or None,
               jobs_created=jobs_created,
               jobs_done=jobs_done,
               jobs_failed=jobs_failed,
               quality_status=quality_status or None)


def fail_run(engine, run_id: int, error_message: str) -> None:
    """Mark run as FAILED."""
    transition(engine, run_id, "FAILED", error_message=error_message)


def get_latest_run(engine) -> dict | None:
    """Get the most recent run.

    Args:
        engine: JobEngine instance.

    Returns:
        Dict with run details, or None if no runs exist.
    """
    _ensure_table(engine)

    conn = engine._get_conn()
    try:
        cur = conn.execute(
            "SELECT run_id, reference_date, started_at, finished_at, status, "
            "plan_hash, jobs_created, jobs_done, jobs_failed, quality_status, error_message, job_ids "
            "FROM update_runs ORDER BY run_id DESC LIMIT 1"
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
            "SELECT run_id, reference_date, started_at, finished_at, status, "
            "plan_hash, jobs_created, jobs_done, jobs_failed, quality_status, error_message, job_ids "
            "FROM update_runs WHERE status='SUCCESS' ORDER BY run_id DESC LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    finally:
        conn.close()


def get_run(engine, run_id: int) -> dict | None:
    """Get a specific run by id.

    Args:
        engine: JobEngine instance.
        run_id: The run id.

    Returns:
        Dict with run details, or None if not found.
    """
    conn = engine._get_conn()
    try:
        cur = conn.execute(
            "SELECT run_id, reference_date, started_at, finished_at, status, "
            "plan_hash, jobs_created, jobs_done, jobs_failed, quality_status, error_message, job_ids "
            "FROM update_runs WHERE run_id=?",
            (run_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    finally:
        conn.close()


def get_runs(engine, limit: int = 10) -> list[dict]:
    """Get recent runs.

    Args:
        engine: JobEngine instance.
        limit: Max runs to return.

    Returns:
        List of run dicts, most recent first.
    """
    conn = engine._get_conn()
    try:
        cur = conn.execute(
            "SELECT run_id, reference_date, started_at, finished_at, status, "
            "plan_hash, jobs_created, jobs_done, jobs_failed, quality_status, error_message, job_ids "
            "FROM update_runs ORDER BY run_id DESC LIMIT ?",
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


# ── Persistent trigger state (P9.4 Step 3) ─────────────────────────────
#
# The update-cycle trigger *timing* used to be process-local in-memory state
# (``UpdateCycleScheduler.next_due``); a service restart reset it to "never
# triggered" so the first tick after boot re-fired a cycle. Step 3 persists
# the trigger instant durably so a reconstructed service/scheduler recovers
# the last trigger across restarts.
#
# The state lives in its OWN dedicated table (``update_cycle_state``) inside
# the SAME JobEngine SQLite database — not in ``update_runs``. The two are
# different entity types: ``update_runs`` holds actual lifecycle runs and is
# read by generic readers (``get_latest_run`` / ``get_runs`` / ``get_run``)
# that must never surface scheduler control metadata; ``update_cycle_state``
# is scheduler control metadata only (a singleton row keyed by ``key``,
# ``last_triggered_at`` = ISO-8601 UTC instant).
#
# Initialization is idempotent (``CREATE TABLE IF NOT EXISTS`` + an upsert),
# so a fresh database, an already-initialized database, and a legacy local
# dev database all converge without destructive migration — P9.4 Step 3 never
# shipped, so no migration framework is warranted. A legacy ``update_runs``
# row written by an uncommitted Step 3 build (``run_id=0``,
# ``status='STATE'``) is inert: no code reads it.
_TRIGGER_STATE_KEY = "update_cycle"


def _ensure_trigger_state_table(conn) -> None:
    """Create the dedicated ``update_cycle_state`` table if it doesn't exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS update_cycle_state (
            key TEXT PRIMARY KEY,
            last_triggered_at TEXT
        )
        """
    )


def save_trigger_state(engine, last_triggered_at: str | None) -> None:
    """Persist the last trigger instant for the update cycle.

    Opens the engine's JobEngine SQLite DB (the same database the run
    lifecycle uses), ensures the dedicated ``update_cycle_state`` table
    exists, then upserts the singleton row.

    Args:
        engine: JobEngine instance.
        last_triggered_at: ISO-8601 UTC instant, or ``None`` for "never
            triggered" (the first-run state).
    """
    conn = engine._get_conn()
    try:
        _ensure_trigger_state_table(conn)
        conn.execute(
            """
            INSERT INTO update_cycle_state (key, last_triggered_at)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                last_triggered_at = excluded.last_triggered_at
            """,
            (_TRIGGER_STATE_KEY, last_triggered_at),
        )
        conn.commit()
    finally:
        conn.close()


def load_trigger_state(engine) -> str | None:
    """Load the persisted last trigger instant from the engine's DB.

    Returns:
        The ISO-8601 UTC instant string, or ``None`` when no trigger state
        exists (first run) — also returned for a corrupt value, so a
        malformed state degrades to first-run semantics rather than crashing.
    """
    conn = engine._get_conn()
    try:
        _ensure_trigger_state_table(conn)
        row = conn.execute(
            "SELECT last_triggered_at FROM update_cycle_state WHERE key=?",
            (_TRIGGER_STATE_KEY,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    value = row[0]
    if not value:
        return None
    try:
        # Reject a corrupt timestamp instead of carrying it forward.
        datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return value