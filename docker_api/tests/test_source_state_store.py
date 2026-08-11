"""Tests for SQLiteSourceStateStore (Subtask 6A).

Verifies:
A. New DB load → None
B. save CLOSED state → load content consistent
C. save OPEN with failures=2, open_until=1030 → load consistent
D. Save same source again → updates record, no duplicate
E. mootdx / baidu states independent
F. Same source different capabilities independent
G. HALF_OPEN can be saved and loaded
H. open_until=None correctly saved/loaded
I. Close and recreate store instance → state persists
J. SQLite file exists at test temp path
"""

import os
import tempfile
import pytest

from astock_api.source_state_store import SQLiteSourceStateStore


class TestSQLiteSourceStateStore:
    def test_new_db_load_returns_none(self):
        """A. New DB load → None."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            store = SQLiteSourceStateStore(db_path)

            result = store.load("mootdx", "market_bars_daily")
            assert result is None

    def test_save_and_load_closed(self):
        """B. save CLOSED state → load content consistent."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            store = SQLiteSourceStateStore(db_path)

            state = {
                "consecutive_failures": 0,
                "state": "CLOSED",
                "open_until": None,
            }
            store.save("mootdx", "market_bars_daily", state)

            loaded = store.load("mootdx", "market_bars_daily")
            assert loaded is not None
            assert loaded["consecutive_failures"] == 0
            assert loaded["state"] == "CLOSED"
            assert loaded["open_until"] is None

    def test_save_and_load_open(self):
        """C. save OPEN: failures=2, open_until=1030 → load consistent."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            store = SQLiteSourceStateStore(db_path)

            state = {
                "consecutive_failures": 2,
                "state": "OPEN",
                "open_until": 1030.0,
            }
            store.save("mootdx", "market_bars_daily", state)

            loaded = store.load("mootdx", "market_bars_daily")
            assert loaded is not None
            assert loaded["consecutive_failures"] == 2
            assert loaded["state"] == "OPEN"
            assert loaded["open_until"] == 1030.0

    def test_save_same_source_updates_not_duplicates(self):
        """D. Save same source again → updates record, no duplicate."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            store = SQLiteSourceStateStore(db_path)

            # First save: CLOSED
            store.save("mootdx", "market_bars_daily", {
                "consecutive_failures": 0,
                "state": "CLOSED",
                "open_until": None,
            })

            # Second save: OPEN
            store.save("mootdx", "market_bars_daily", {
                "consecutive_failures": 2,
                "state": "OPEN",
                "open_until": 1030.0,
            })

            loaded = store.load("mootdx", "market_bars_daily")
            assert loaded["state"] == "OPEN"
            assert loaded["consecutive_failures"] == 2

    def test_mootdx_baidu_independent(self):
        """E. mootdx / baidu states independent."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            store = SQLiteSourceStateStore(db_path)

            store.save("mootdx", "market_bars_daily", {
                "consecutive_failures": 2,
                "state": "OPEN",
                "open_until": 1030.0,
            })

            store.save("baidu", "market_bars_daily", {
                "consecutive_failures": 0,
                "state": "CLOSED",
                "open_until": None,
            })

            mootdx = store.load("mootdx", "market_bars_daily")
            baidu = store.load("baidu", "market_bars_daily")

            assert mootdx["state"] == "OPEN"
            assert baidu["state"] == "CLOSED"

    def test_same_source_different_capability_independent(self):
        """F. Same source different capabilities independent."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            store = SQLiteSourceStateStore(db_path)

            store.save("mootdx", "market_bars_daily", {
                "consecutive_failures": 2,
                "state": "OPEN",
                "open_until": 1030.0,
            })

            store.save("mootdx", "market_bars_weekly", {
                "consecutive_failures": 0,
                "state": "CLOSED",
                "open_until": None,
            })

            daily = store.load("mootdx", "market_bars_daily")
            weekly = store.load("mootdx", "market_bars_weekly")

            assert daily["state"] == "OPEN"
            assert weekly["state"] == "CLOSED"

    def test_half_open_save_and_load(self):
        """G. HALF_OPEN can be saved and loaded."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            store = SQLiteSourceStateStore(db_path)

            state = {
                "consecutive_failures": 2,
                "state": "HALF_OPEN",
                "open_until": None,
            }
            store.save("mootdx", "market_bars_daily", state)

            loaded = store.load("mootdx", "market_bars_daily")
            assert loaded is not None
            assert loaded["state"] == "HALF_OPEN"

    def test_open_until_none_correctly_saved(self):
        """H. open_until=None correctly saved/loaded."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            store = SQLiteSourceStateStore(db_path)

            state = {
                "consecutive_failures": 0,
                "state": "CLOSED",
                "open_until": None,
            }
            store.save("mootdx", "market_bars_daily", state)

            loaded = store.load("mootdx", "market_bars_daily")
            assert loaded["open_until"] is None

    def test_state_persists_across_instances(self):
        """I. Close and recreate store instance → state persists."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")

            # First instance saves state
            store1 = SQLiteSourceStateStore(db_path)
            store1.save("mootdx", "market_bars_daily", {
                "consecutive_failures": 2,
                "state": "OPEN",
                "open_until": 1030.0,
            })

            # Second instance loads same state
            store2 = SQLiteSourceStateStore(db_path)
            loaded = store2.load("mootdx", "market_bars_daily")

            assert loaded is not None
            assert loaded["state"] == "OPEN"
            assert loaded["consecutive_failures"] == 2
            assert loaded["open_until"] == 1030.0

    def test_sqlite_file_exists(self):
        """J. SQLite file exists at test temp path."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            store = SQLiteSourceStateStore(db_path)

            store.save("mootdx", "market_bars_daily", {
                "consecutive_failures": 0,
                "state": "CLOSED",
                "open_until": None,
            })

            assert os.path.exists(db_path)
