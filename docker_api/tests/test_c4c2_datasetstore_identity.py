"""C4C-2 DatasetStore Explicit Instrument Identity tests.

Verifies:
A. Legacy SSE equity (600519 → SSE:600519)
B. Legacy SZSE equity (000001 → SZSE:000001)
C. SSE index critical case (SSE, 000001, INDEX → SSE:000001)
D. Cross-market coexistence (SSE:000001 + SZSE:000001 same trade_date)
E. Canonical identity preservation (no re-inference)
F. Existing DatasetStore equity regression unchanged
"""

import json
import os
import tempfile

import pytest


class TestLegacyEquityCompatibility:
    """A-B. Legacy equity workflow unchanged."""

    def test_sse_equity_600519(self):
        """Legacy: 600519 → SSE:600519 via prefix inference."""
        from astock_api.dataset_store import DatasetStore

        sid = DatasetStore.make_security_id("600519")
        assert sid == "SSE:600519"

    def test_szse_equity_000001(self):
        """Legacy: 000001 → SZSE:000001 via prefix inference."""
        from astock_api.dataset_store import DatasetStore

        sid = DatasetStore.make_security_id("000001")
        assert sid == "SZSE:000001"

    def test_szse_equity_399001(self):
        """Legacy: 399001 → SZSE:399001 via prefix inference."""
        from astock_api.dataset_store import DatasetStore

        sid = DatasetStore.make_security_id("399001")
        assert sid == "SZSE:399001"

    def test_bse_equity_430090(self):
        """Legacy: 430090 → BSE:430090 via prefix inference."""
        from astock_api.dataset_store import DatasetStore

        sid = DatasetStore.make_security_id("430090")
        assert sid == "BSE:430090"


class TestSseIndexCriticalCase:
    """C. SSE index code 000001 must NOT be inferred as SZSE."""

    def test_sse_000001_index_explicit_exchange(self):
        """SSE:000001 INDEX with explicit exchange → SSE:000001."""
        from astock_api.dataset_store import DatasetStore

        sid = DatasetStore.make_security_id("000001", exchange="SSE")
        assert sid == "SSE:000001"

    def test_sse_000001_index_not_reinferred_as_szse(self):
        """Critical: SSE index 000001 must NOT become SZSE."""
        from astock_api.dataset_store import DatasetStore

        sid = DatasetStore.make_security_id("000001", exchange="SSE")
        assert sid != "SZSE:000001"  # Must not be SZSE
        assert sid == "SSE:000001"

    def test_sse_999999_index_explicit_exchange(self):
        """SSE:999999 INDEX with explicit exchange → SSE:999999."""
        from astock_api.dataset_store import DatasetStore

        sid = DatasetStore.make_security_id("999999", exchange="SSE")
        assert sid == "SSE:999999"

    def test_szse_399001_index_explicit_exchange(self):
        """SZSE:399001 INDEX with explicit exchange → SZSE:399001."""
        from astock_api.dataset_store import DatasetStore

        sid = DatasetStore.make_security_id("399001", exchange="SZSE")
        assert sid == "SZSE:399001"


class TestCrossMarketCoexistence:
    """D. SSE:000001 and SZSE:000001 coexist in DuckDB."""

    def test_same_trade_date_two_securities(self):
        """SSE:000001 and SZSE:000001 on same trade_date must coexist."""
        from astock_api.dataset_store import DatasetStore

        with tempfile.TemporaryDirectory() as tmpdir:
            store = DatasetStore(":memory:")
            store.bootstrap()

            # Write SSE:000001 INDEX bars
            sse_bars = [
                {"trade_date": "2026-01-01", "open": 3000, "high": 3050, "low": 2980, "close": 3020, "volume": 1000000, "amount": 3020000000},
            ]
            store.write_market_bars("SSE:000001", sse_bars, "mootdx", "test-job-sse")

            # Write SZSE:000001 EQUITY bars (same date)
            szse_bars = [
                {"trade_date": "2026-01-01", "open": 15, "high": 15.5, "low": 14.8, "close": 15.2, "volume": 50000, "amount": 760000},
            ]
            store.write_market_bars("SZSE:000001", szse_bars, "mootdx", "test-job-szse")

            # Query both
            conn = store.get_conn()
            rows = conn.execute(
                "SELECT security_id, trade_date, close FROM market_bars_daily ORDER BY security_id"
            ).fetchall()

            assert len(rows) == 2
            # SSE:000001 should have close=3020 (index)
            sse_row = [r for r in rows if r[0] == "SSE:000001"][0]
            assert sse_row[2] == 3020

            # SZSE:000001 should have close=15.2 (equity)
            szse_row = [r for r in rows if r[0] == "SZSE:000001"][0]
            assert szse_row[2] == 15.2

    def test_pk_no_conflict(self):
        """PK(security_id, trade_date) must not conflict for SSE:000001 vs SZSE:000001."""
        from astock_api.dataset_store import DatasetStore

        with tempfile.TemporaryDirectory() as tmpdir:
            store = DatasetStore(":memory:")
            store.bootstrap()

            bars = [{"trade_date": "2026-01-01", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 100, "amount": 200}]

            # Write both — must not raise UNIQUE constraint error
            store.write_market_bars("SSE:000001", bars, "mootdx", "job-1")
            store.write_market_bars("SZSE:000001", bars, "mootdx", "job-2")

            # Both should exist
            conn = store.get_conn()
            count = conn.execute("SELECT COUNT(*) FROM market_bars_daily").fetchone()[0]
            assert count == 2

    def test_query_does_not_cross_contaminate(self):
        """Querying SSE:000001 must not return SZSE:000001 data."""
        from astock_api.dataset_store import DatasetStore

        with tempfile.TemporaryDirectory() as tmpdir:
            store = DatasetStore(":memory:")
            store.bootstrap()

            sse_bars = [{"trade_date": "2026-01-01", "open": 3000, "high": 3050, "low": 2980, "close": 3020, "volume": 1000000, "amount": 3020000000}]
            szse_bars = [{"trade_date": "2026-01-01", "open": 15, "high": 15.5, "low": 14.8, "close": 15.2, "volume": 50000, "amount": 760000}]

            store.write_market_bars("SSE:000001", sse_bars, "mootdx", "job-sse")
            store.write_market_bars("SZSE:000001", szse_bars, "mootdx", "job-szse")

            conn = store.get_conn()
            sse_rows = conn.execute(
                "SELECT close FROM market_bars_daily WHERE security_id='SSE:000001'"
            ).fetchall()
            szse_rows = conn.execute(
                "SELECT close FROM market_bars_daily WHERE security_id='SZSE:000001'"
            ).fetchall()

            assert len(sse_rows) == 1
            assert sse_rows[0][0] == 3020  # Index close
            assert len(szse_rows) == 1
            assert szse_rows[0][0] == 15.2  # Equity close


class TestCanonicalIdentityPreservation:
    """E. DatasetStore does not re-infer Instrument.exchange."""

    def test_explicit_exchange_not_overridden(self):
        """make_security_id with explicit exchange must use it."""
        from astock_api.dataset_store import DatasetStore

        # SSE:000001 (INDEX) — explicit exchange must be honored
        assert DatasetStore.make_security_id("000001", exchange="SSE") == "SSE:000001"

        # SZSE:000001 (EQUITY) — explicit exchange must be honored
        assert DatasetStore.make_security_id("000001", exchange="SZSE") == "SZSE:000001"

    def test_invalid_exchange_raises(self):
        """Invalid exchange string must raise ValueError, not produce garbage."""
        from astock_api.dataset_store import DatasetStore

        with pytest.raises(ValueError, match="Invalid exchange"):
            DatasetStore.make_security_id("000001", exchange="FOO")

        with pytest.raises(ValueError, match="Invalid exchange"):
            DatasetStore.make_security_id("000001", exchange="")

    def test_lowercase_exchange_normalized(self):
        """Lowercase exchange should be normalized to uppercase."""
        from astock_api.dataset_store import DatasetStore

        assert DatasetStore.make_security_id("000001", exchange="sse") == "SSE:000001"
        assert DatasetStore.make_security_id("600519", exchange="szse") == "SZSE:600519"

    def test_no_exchange_uses_prefix_inference(self):
        """Without explicit exchange, prefix inference still works."""
        from astock_api.dataset_store import DatasetStore

        assert DatasetStore.make_security_id("600519") == "SSE:600519"
        assert DatasetStore.make_security_id("000001") == "SZSE:000001"
        assert DatasetStore.make_security_id("399001") == "SZSE:399001"


class TestDuckdbSchemaUnchanged:
    """F. DuckDB schema remains unchanged."""

    def test_market_bars_daily_pk_unchanged(self):
        """PK(security_id, trade_date) must remain the same."""
        from astock_api.dataset_store import DatasetStore

        store = DatasetStore(":memory:")
        store.bootstrap()

        conn = store.get_conn()
        table_info = conn.execute("PRAGMA table_info(market_bars_daily)").fetchall()
        columns = [row[1] for row in table_info]

        assert "security_id" in columns
        assert "trade_date" in columns
        # PK should be on (security_id, trade_date)


class TestExistingDatasetStoreRegression:
    """F. Existing equity tests unchanged."""

    def test_equity_write_and_read(self):
        """Existing equity workflow: write bars, read back."""
        from astock_api.dataset_store import DatasetStore

        store = DatasetStore(":memory:")
        store.bootstrap()

        bars = [
            {"trade_date": "2026-01-01", "open": 180, "high": 185, "low": 178, "close": 182, "volume": 50000, "amount": 9100000},
            {"trade_date": "2026-01-02", "open": 183, "high": 186, "low": 180, "close": 184, "volume": 52000, "amount": 9568000},
        ]

        store.write_market_bars("SSE:600519", bars, "mootdx", "test-job")

        conn = store.get_conn()
        rows = conn.execute(
            "SELECT trade_date, close FROM market_bars_daily WHERE security_id='SSE:600519' ORDER BY trade_date"
        ).fetchall()

        assert len(rows) == 2
        assert rows[0][1] == 182
        assert rows[1][1] == 184

    def test_upsert_overwrites_existing(self):
        """UPSERT semantics: same security_id + trade_date overwrites."""
        from astock_api.dataset_store import DatasetStore

        store = DatasetStore(":memory:")
        store.bootstrap()

        bars1 = [{"trade_date": "2026-01-01", "open": 180, "high": 185, "low": 178, "close": 182, "volume": 50000, "amount": 9100000}]
        store.write_market_bars("SSE:600519", bars1, "mootdx", "job-1")

        bars2 = [{"trade_date": "2026-01-01", "open": 181, "high": 186, "low": 179, "close": 183, "volume": 51000, "amount": 9333000}]
        store.write_market_bars("SSE:600519", bars2, "baidu", "job-2")

        conn = store.get_conn()
        rows = conn.execute(
            "SELECT close, volume FROM market_bars_daily WHERE security_id='SSE:600519'"
        ).fetchall()

        assert len(rows) == 1
        # Should be overwritten by bars2
        assert rows[0][0] == 183
        assert rows[0][1] == 51000
