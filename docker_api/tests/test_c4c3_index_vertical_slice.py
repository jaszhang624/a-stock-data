"""R5-C4C-3: Index Vertical Slice — targeted tests.

Tests the complete INDEX path:
  API input → JobEngine create_job → Instrument identity → Handler payload
  → Source adapter routing (mootdx index_bars) → DatasetStore security_id

Coverage:
  A. Legacy SSE equity (symbols=["600519"]) → SSE:600519 EQUITY
  B. Legacy SZSE equity (symbols=["000001"]) → SZSE:000001 EQUITY
  C. Structured SSE/000001/INDEX → SSE:000001 canonical chunk_key
  D. INDEX → mootdx index_bars method (mocked)
  E. EQUITY → mootdx bars method (mocked)
  F. SSE:000001 INDEX → DatasetStore security_id=SSE:000001
  G. Bare ambiguous INDEX without exchange → rejected
  H. SSE:000001 INDEX + SZSE:000001 EQUITY identity isolation
"""
import json
import os
import shutil
import tempfile

import pytest
from unittest.mock import MagicMock, patch


class TestC4C3IndexVerticalSlice:

    @pytest.fixture(autouse=True)
    def setup(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_jobs.db")
        self.data_dir = self.tmpdir

        from astock_api.job_engine import JobEngine
        self.engine = JobEngine(db_path=self.db_path, data_dir=self.data_dir)
        self.engine.initialize()

        yield

        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ── A. Legacy SSE equity ────────────────────────────────────────

    def test_a_legacy_sse_equity(self):
        """symbols=["600519"] → SSE:600519 EQUITY (unchanged)"""
        result = self.engine.create_job("market_bars_snapshot", {
            "symbols": ["600519"],
            "frequency": "daily",
            "count": 100,
        })

        job = self.engine.get_job(result["job_id"])
        assert job["total_chunks"] == 1

        chunks = self.engine.get_chunks(result["job_id"])
        assert len(chunks) == 1

        payload = json.loads(chunks[0]["payload_json"])
        assert payload["symbol"] == "600519"
        assert payload["canonical_id"] == "SSE:600519"
        assert payload["exchange"] == "SSE"
        assert payload["asset_type"] == "EQUITY"

    # ── B. Legacy SZSE equity ───────────────────────────────────────

    def test_b_legacy_szse_equity(self):
        """symbols=["000001"] → SZSE:000001 EQUITY (unchanged)"""
        result = self.engine.create_job("market_bars_snapshot", {
            "symbols": ["000001"],
            "frequency": "daily",
            "count": 100,
        })

        chunks = self.engine.get_chunks(result["job_id"])
        payload = json.loads(chunks[0]["payload_json"])

        assert payload["symbol"] == "000001"
        assert payload["canonical_id"] == "SZSE:000001"
        assert payload["exchange"] == "SZSE"
        assert payload["asset_type"] == "EQUITY"

    # ── C. Structured INDEX input ───────────────────────────────────

    def test_c_structured_sse_index(self):
        """instruments=[{code:000001, exchange:SSE, asset_type:INDEX}] → SSE:000001"""
        result = self.engine.create_job("market_bars_snapshot", {
            "instruments": [
                {"code": "000001", "exchange": "SSE", "asset_type": "INDEX"}
            ],
            "frequency": "daily",
            "count": 3,
        })

        job = self.engine.get_job(result["job_id"])
        assert job["total_chunks"] == 1

        chunks = self.engine.get_chunks(result["job_id"])
        assert len(chunks) == 1

        # Verify chunk_key uses canonical_id
        assert "SSE:000001" in chunks[0]["chunk_key"]

        payload = json.loads(chunks[0]["payload_json"])
        assert payload["symbol"] == "000001"
        assert payload["canonical_id"] == "SSE:000001"
        assert payload["exchange"] == "SSE"
        assert payload["asset_type"] == "INDEX"

    def test_c2_structured_szse_index(self):
        """instruments=[{code:399001, exchange:SZSE, asset_type:INDEX}] → SZSE:399001"""
        result = self.engine.create_job("market_bars_snapshot", {
            "instruments": [
                {"code": "399001", "exchange": "SZSE", "asset_type": "INDEX"}
            ],
            "frequency": "daily",
            "count": 3,
        })

        chunks = self.engine.get_chunks(result["job_id"])
        payload = json.loads(chunks[0]["payload_json"])

        assert payload["canonical_id"] == "SZSE:399001"
        assert payload["exchange"] == "SZSE"
        assert payload["asset_type"] == "INDEX"

    # ── D. INDEX → mootdx index_bars method ────────────────────────

    def test_d_index_routes_to_mootdx_index_bars(self):
        """INDEX instrument → mootdx.index_bars() (not bars())"""
        from astock_api.source_adapters import MootdxSource
        from astock_api.instrument import Instrument

        source = MootdxSource()

        # Mock client
        mock_client = MagicMock()
        mock_df = MagicMock()
        mock_df.empty = False
        mock_df.columns = ["datetime", "open", "high", "low", "close", "volume", "amount"]
        mock_df.tail.return_value = mock_df
        mock_df.to_dict.return_value = [{"datetime": "2025-01-01", "open": 1.0, "high": 2.0, "low": 1.5, "close": 1.8, "volume": 100, "amount": 1000}]
        mock_client.index_bars.return_value = mock_df

        with patch.object(source, '_get_client', return_value=mock_client):
            instrument = Instrument(exchange="SSE", code="000001", asset_type="INDEX")
            result = source.fetch_market_bars(instrument, "daily", 3)

        # Verify index_bars was called (not bars)
        mock_client.index_bars.assert_called_once()
        mock_client.bars.assert_not_called()

        assert result["canonical_id"] == "SSE:000001"
        assert result["exchange"] == "SSE"

    # ── E. EQUITY → mootdx bars method ─────────────────────────────

    def test_e_equity_routes_to_mootdx_bars(self):
        """EQUITY instrument → mootdx.bars() (not index_bars())"""
        from astock_api.source_adapters import MootdxSource
        from astock_api.instrument import Instrument

        source = MootdxSource()

        mock_client = MagicMock()
        mock_df = MagicMock()
        mock_df.empty = False
        mock_df.columns = ["datetime", "open", "high", "low", "close", "volume", "amount"]
        mock_df.tail.return_value = mock_df
        mock_df.to_dict.return_value = [{"datetime": "2025-01-01", "open": 1.0, "high": 2.0, "low": 1.5, "close": 1.8, "volume": 100, "amount": 1000}]
        mock_client.bars.return_value = mock_df

        with patch.object(source, '_get_client', return_value=mock_client):
            instrument = Instrument(exchange="SSE", code="600519", asset_type="EQUITY")
            result = source.fetch_market_bars(instrument, "daily", 3)

        mock_client.bars.assert_called_once()
        mock_client.index_bars.assert_not_called()

        assert result["canonical_id"] == "SSE:600519"

    # ── F. INDEX → DatasetStore security_id preservation ───────────

    def test_f_index_datasetstore_security_id(self):
        """SSE:000001 INDEX → DatasetStore writes security_id=SSE:000001"""
        from astock_api.dataset_store import DatasetStore

        store = DatasetStore(":memory:")
        store.bootstrap()

        # Write INDEX bars with explicit exchange
        security_id = DatasetStore.make_security_id("000001", exchange="SSE")
        assert security_id == "SSE:000001"

        bars = [
            {"trade_date": "2025-01-01", "open": 1.0, "high": 2.0, "low": 1.5, "close": 1.8, "volume": 100, "amount": 1000},
        ]
        store.write_market_bars(security_id, bars, "mootdx", "test-job")

        # Verify the row was written with correct security_id
        conn = store.get_conn()
        try:
            row = conn.execute(
                "SELECT security_id, trade_date FROM market_bars_daily WHERE security_id=?",
                ("SSE:000001",)
            ).fetchone()
            assert row is not None, "SSE:000001 should exist in DuckDB"
            assert row[0] == "SSE:000001"
        finally:
            conn.close()

    # ── G. Ambiguous INDEX without exchange → rejected ─────────────

    def test_g_ambiguous_index_rejected(self):
        """INDEX without exchange → ValueError"""
        with pytest.raises(ValueError, match="exchange must be specified for INDEX"):
            self.engine.create_job("market_bars_snapshot", {
                "instruments": [
                    {"code": "000001", "asset_type": "INDEX"}  # no exchange
                ],
                "frequency": "daily",
                "count": 3,
            })

    def test_g2_ambiguous_index_via_instrument(self):
        """parse_instrument(000001, asset_type=INDEX) without exchange → AmbiguousExchangeError"""
        from astock_api.instrument import parse_instrument, AmbiguousExchangeError

        with pytest.raises(AmbiguousExchangeError):
            parse_instrument("000001", asset_type="INDEX")

    # ── H. Cross-market identity isolation ─────────────────────────

    def test_h_cross_market_identity_isolation(self):
        """SSE:000001 INDEX and SZSE:000001 EQUITY have distinct identities"""
        from astock_api.instrument import Instrument

        index_inst = Instrument(exchange="SSE", code="000001", asset_type="INDEX")
        equity_inst = Instrument(exchange="SZSE", code="000001", asset_type="EQUITY")

        assert index_inst.canonical_id == "SSE:000001"
        assert equity_inst.canonical_id == "SZSE:000001"
        assert index_inst.canonical_id != equity_inst.canonical_id

    def test_h2_cross_market_coexistence_in_duckdb(self):
        """SSE:000001 INDEX and SZSE:000001 EQUITY coexist in DuckDB"""
        from astock_api.dataset_store import DatasetStore

        store = DatasetStore(":memory:")
        store.bootstrap()

        bars = [{"trade_date": "2025-01-01", "open": 1.0, "high": 2.0, "low": 1.5, "close": 1.8, "volume": 100, "amount": 1000}]

        # Write SSE:000001 INDEX
        store.write_market_bars("SSE:000001", bars, "mootdx", "test-index")
        # Write SZSE:000001 EQUITY
        store.write_market_bars("SZSE:000001", bars, "mootdx", "test-equity")

        conn = store.get_conn()
        try:
            rows = conn.execute(
                "SELECT security_id FROM market_bars_daily WHERE trade_date='2025-01-01' ORDER BY security_id"
            ).fetchall()

            assert len(rows) == 2, "Both SSE:000001 and SZSE:000001 should coexist"
            security_ids = [r[0] for r in rows]
            assert "SSE:000001" in security_ids
            assert "SZSE:000001" in security_ids

            # Verify query isolation
            index_rows = conn.execute(
                "SELECT * FROM market_bars_daily WHERE security_id='SSE:000001'"
            ).fetchall()
            equity_rows = conn.execute(
                "SELECT * FROM market_bars_daily WHERE security_id='SZSE:000001'"
            ).fetchall()

            assert len(index_rows) == 1
            assert len(equity_rows) == 1
        finally:
            conn.close()

    # ── I. Handler payload preserves INDEX identity ────────────────

    def test_i_handler_preserves_index_identity(self):
        """Handler reads exchange/asset_type from payload for INDEX"""
        from astock_api.job_handlers import market_bars_handler

        # Mock governor to return a result
        mock_governor = MagicMock()
        mock_governor.fetch_market_bars.return_value = {
            "source": "mootdx",
            "symbol": "000001",
            "canonical_id": "SSE:000001",
            "exchange": "SSE",
            "frequency": "daily",
            "requested_count": 3,
            "rows": [],
        }

        payload = {
            "symbol": "000001",
            "canonical_id": "SSE:000001",
            "exchange": "SSE",
            "asset_type": "INDEX",
            "frequency": "daily",
            "count": 3,
        }

        with patch("astock_api.job_handlers.get_governor", return_value=mock_governor):
            result = market_bars_handler(payload)

        # Verify governor was called with INDEX instrument
        call_args = mock_governor.fetch_market_bars.call_args
        instrument = call_args[0][0]

        assert instrument.exchange == "SSE"
        assert instrument.code == "000001"
        assert instrument.asset_type == "INDEX"
        assert instrument.canonical_id == "SSE:000001"

    def test_i2_handler_legacy_equity_fallback(self):
        """Handler falls back to EQUITY when payload lacks exchange/asset_type"""
        from astock_api.job_handlers import market_bars_handler

        mock_governor = MagicMock()
        mock_governor.fetch_market_bars.return_value = {
            "source": "mootdx",
            "symbol": "600519",
            "canonical_id": "SSE:600519",
            "exchange": "SSE",
            "frequency": "daily",
            "requested_count": 100,
            "rows": [],
        }

        # Legacy payload without exchange/asset_type
        payload = {
            "symbol": "600519",
            "frequency": "daily",
            "count": 100,
        }

        with patch("astock_api.job_handlers.get_governor", return_value=mock_governor):
            result = market_bars_handler(payload)

        call_args = mock_governor.fetch_market_bars.call_args
        instrument = call_args[0][0]

        assert instrument.exchange == "SSE"
        assert instrument.asset_type == "EQUITY"

    # ── J. Mixed instruments in single job ─────────────────────────

    def test_j_mixed_equity_and_index_instruments(self):
        """Job with both EQUITY and INDEX instruments"""
        result = self.engine.create_job("market_bars_snapshot", {
            "instruments": [
                {"code": "600519", "exchange": "SSE", "asset_type": "EQUITY"},
                {"code": "000001", "exchange": "SSE", "asset_type": "INDEX"},
                {"code": "000001", "exchange": "SZSE", "asset_type": "EQUITY"},
            ],
            "frequency": "daily",
            "count": 3,
        })

        job = self.engine.get_job(result["job_id"])
        assert job["total_chunks"] == 3

        chunks = self.engine.get_chunks(result["job_id"])
        canonical_ids = []
        for c in chunks:
            payload = json.loads(c["payload_json"])
            canonical_ids.append(payload["canonical_id"])

        assert "SSE:600519" in canonical_ids
        assert "SSE:000001" in canonical_ids
        assert "SZSE:000001" in canonical_ids
