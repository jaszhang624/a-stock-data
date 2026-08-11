"""SQLite-backed source health state store.

Persists source governor health state (consecutive_failures, OPEN/CLOSED/HALF_OPEN)
to a local SQLite database so that state survives process restarts.

Not yet wired into SourceGovernor — this is a standalone store for Subtask 6A.
"""

import sqlite3
import time


class SQLiteSourceStateStore:
    """Simple SQLite store for source health state.

    Key: (source_name, capability)
    State: {consecutive_failures, state, open_until}

    Each operation opens a new connection — no long-lived connections.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Create a new connection with pragmas."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        """Create table if not exists."""
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS source_state (
                    source_name   TEXT NOT NULL,
                    capability    TEXT NOT NULL,
                    state         TEXT NOT NULL DEFAULT 'CLOSED',
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    open_until    REAL,
                    updated_at    REAL NOT NULL,
                    PRIMARY KEY (source_name, capability)
                )
            """)

    def load(self, source_name: str, capability: str) -> dict | None:
        """Load health state for a source+capability.

        Returns None if no record exists.
        """
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT state, consecutive_failures, open_until, updated_at FROM source_state WHERE source_name = ? AND capability = ?",
                (source_name, capability),
            ).fetchone()

        if row is None:
            return None

        state, failures, open_until, updated_at = row
        return {
            "consecutive_failures": failures,
            "state": state,
            "open_until": open_until,
        }

    def save(self, source_name: str, capability: str, state: dict) -> None:
        """Save or update health state for a source+capability.

        Upsert semantics — creates record if missing, updates if exists.
        """
        now = time.time()

        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO source_state (source_name, capability, state, consecutive_failures, open_until, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_name, capability) DO UPDATE SET
                    state = excluded.state,
                    consecutive_failures = excluded.consecutive_failures,
                    open_until = excluded.open_until,
                    updated_at = excluded.updated_at
                """,
                (
                    source_name,
                    capability,
                    state["state"],
                    state.get("consecutive_failures", 0),
                    state.get("open_until"),
                    now,
                ),
            )

    def delete(self, source_name: str, capability: str) -> None:
        """Delete a source state record."""
        with self._get_conn() as conn:
            conn.execute(
                "DELETE FROM source_state WHERE source_name = ? AND capability = ?",
                (source_name, capability),
            )
