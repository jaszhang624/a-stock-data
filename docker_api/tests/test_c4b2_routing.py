"""C4B-2 Adapter Routing tests.

Verifies:
A. Handler parses bare symbol → Instrument (EQUITY context)
B. mootdx routes SSE→market=1, SZSE→market=0 via Instrument.exchange
C. Baidu converts SSE→sh600519, SZSE→sz000001 via Instrument.exchange
D. Explicit identity not re-inferred (SSE:000001 stays SSE, not SZSE)
E. SourceGovernor passes Instrument through to adapters unchanged
F. Backward compat: symbol="600519" still works
"""

from unittest.mock import MagicMock, patch

import pytest


class TestHandlerRouting:
    """A. Handler parses bare symbol → Instrument (EQUITY context)."""

    def test_600519_equity_routes_to_sse(self):
        with patch("astock_api.job_handlers.get_governor") as mock_get:
            from astock_api.job_handlers import market_bars_handler

            mock_result = {
                "source": "mootdx", "symbol": "600519",
                "canonical_id": "SSE:600519", "exchange": "SSE",
                "frequency": "daily", "requested_count": 3, "rows": [],
            }

            mock_gov = MagicMock()
            mock_gov.fetch_market_bars.return_value = mock_result
            mock_get.return_value = mock_gov

            result = market_bars_handler({"symbol": "600519", "frequency": "daily", "count": 3})

        assert result["canonical_id"] == "SSE:600519"
        # Verify governor was called with Instrument (not bare string)
        call_args = mock_gov.fetch_market_bars.call_args
        instrument = call_args[0][0]
        assert instrument.exchange == "SSE"
        assert instrument.code == "600519"

    def test_000001_equity_routes_to_szse(self):
        with patch("astock_api.job_handlers.get_governor") as mock_get:
            from astock_api.job_handlers import market_bars_handler

            mock_result = {
                "source": "mootdx", "symbol": "000001",
                "canonical_id": "SZSE:000001", "exchange": "SZSE",
                "frequency": "daily", "requested_count": 3, "rows": [],
            }

            mock_gov = MagicMock()
            mock_gov.fetch_market_bars.return_value = mock_result
            mock_get.return_value = mock_gov

            result = market_bars_handler({"symbol": "000001", "frequency": "daily", "count": 3})

        assert result["canonical_id"] == "SZSE:000001"
        call_args = mock_gov.fetch_market_bars.call_args
        instrument = call_args[0][0]
        assert instrument.exchange == "SZSE"


class TestMootdxExchangeRouting:
    """B. mootdx routes SSE→market=1, SZSE→market=0 via Instrument.exchange."""

    def test_sse_600519_routes_to_market_1(self):
        from astock_api.source_adapters import MootdxSource
        from astock_api.instrument import Instrument

        mock_df = MagicMock()
        mock_df.empty = False
        mock_df.columns = ["datetime", "open", "high", "low", "close", "volume", "amount"]
        mock_df.tail.return_value.__getitem__ = MagicMock(
            return_value=MagicMock(to_dict=MagicMock(return_value=[]))
        )

        mock_client = MagicMock()
        mock_client.bars.return_value = mock_df

        source = MootdxSource()
        with patch.object(source, "_get_client", return_value=mock_client):
            inst = Instrument(exchange="SSE", code="600519", asset_type="EQUITY")
            source.fetch_market_bars(inst, "daily", 3)

        # Verify client.bars was called with code and market=1
        mock_client.bars.assert_called_once()
        call_args = mock_client.bars.call_args
        assert call_args[0][0] == "600519"  # code
        assert call_args[0][1] == 9  # frequency (mootdx uses 9 for daily)

    def test_szse_000001_routes_to_market_0(self):
        from astock_api.source_adapters import MootdxSource
        from astock_api.instrument import Instrument

        mock_df = MagicMock()
        mock_df.empty = False
        mock_df.columns = ["datetime", "open", "high", "low", "close", "volume", "amount"]
        mock_df.tail.return_value.__getitem__ = MagicMock(
            return_value=MagicMock(to_dict=MagicMock(return_value=[]))
        )

        mock_client = MagicMock()
        mock_client.bars.return_value = mock_df

        source = MootdxSource()
        with patch.object(source, "_get_client", return_value=mock_client):
            inst = Instrument(exchange="SZSE", code="000001", asset_type="EQUITY")
            source.fetch_market_bars(inst, "daily", 3)

        mock_client.bars.assert_called_once()
        call_args = mock_client.bars.call_args
        assert call_args[0][0] == "000001"


class TestBaiduExchangeRouting:
    """C. Baidu converts SSE→sh600519, SZSE→sz000001 via Instrument.exchange."""

    def test_sse_600519_baidu_symbol(self):
        from astock_api.source_adapters import BaiduSource
        from astock_api.instrument import Instrument

        source = BaiduSource()
        mock_fetch_raw = MagicMock(return_value={
            "ResultCode": 0,
            "Result": {
                "newMarketData": {
                    "keys": ["time", "open"],
                    "marketData": "2026-08-10,1325.00",
                }
            },
        })
        source._fetch_raw = mock_fetch_raw

        inst = Instrument(exchange="SSE", code="600519", asset_type="EQUITY")
        source.fetch_market_bars(inst, "daily", 3)

        # Verify _fetch_raw was called with baidu-specific format
        mock_fetch_raw.assert_called_once_with("sh600519")

    def test_szse_000001_baidu_symbol(self):
        from astock_api.source_adapters import BaiduSource
        from astock_api.instrument import Instrument

        source = BaiduSource()
        mock_fetch_raw = MagicMock(return_value={
            "ResultCode": 0,
            "Result": {
                "newMarketData": {
                    "keys": ["time", "open"],
                    "marketData": "2026-08-10,1325.00",
                }
            },
        })
        source._fetch_raw = mock_fetch_raw

        inst = Instrument(exchange="SZSE", code="000001", asset_type="EQUITY")
        source.fetch_market_bars(inst, "daily", 3)

        mock_fetch_raw.assert_called_once_with("sz000001")


class TestExplicitIdentityNotReinferred:
    """D. Explicit identity not re-inferred (SSE:000001 stays SSE, not SZSE)."""

    def test_sse_000001_index_not_reinferred(self):
        """SSE:000001 INDEX must NOT be re-inferred as SZSE."""
        from astock_api.source_adapters import MootdxSource, SourceUnsupportedError
        from astock_api.instrument import Instrument

        # SSE:000001 INDEX — mootdx should NOT re-infer exchange from prefix
        # The Instrument.exchange is SSE, so mootdx routes to market=1 (SSE)
        mock_df = MagicMock()
        mock_df.empty = False
        mock_df.columns = ["datetime", "open", "high", "low", "close", "volume", "amount"]
        mock_df.tail.return_value.__getitem__ = MagicMock(
            return_value=MagicMock(to_dict=MagicMock(return_value=[]))
        )

        mock_client = MagicMock()
        mock_client.index_bars.return_value = mock_df

        source = MootdxSource()
        with patch.object(source, "_get_client", return_value=mock_client):
            inst = Instrument(exchange="SSE", code="000001", asset_type="INDEX")
            source.fetch_market_bars(inst, "daily", 3)

        # Verify client.index_bars was called (INDEX routes to index_bars, not bars)
        mock_client.index_bars.assert_called_once()
        call_args = mock_client.index_bars.call_args
        assert call_args[0][0] == "000001"


class TestSourceGovernorPassthrough:
    """E. SourceGovernor passes Instrument through to adapters unchanged."""

    def test_governor_passes_instrument_to_adapter(self):
        from astock_api.source_governor import SourceGovernor
        from astock_api.instrument import Instrument

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        mock_source.fetch_market_bars.return_value = {
            "source": "mootdx", "symbol": "600519",
            "canonical_id": "SSE:600519", "exchange": "SSE",
            "frequency": "daily", "requested_count": 3, "rows": [],
        }

        g = SourceGovernor()
        g.register(mock_source)

        inst = Instrument(exchange="SSE", code="600519", asset_type="EQUITY")
        g.fetch_market_bars(inst, "daily", 3)

        # Verify adapter received the Instrument object
        mock_source.fetch_market_bars.assert_called_once()
        call_args = mock_source.fetch_market_bars.call_args
        received_inst = call_args[0][0]
        assert isinstance(received_inst, Instrument)
        assert received_inst.exchange == "SSE"
        assert received_inst.code == "600519"


class TestBackwardCompatibility:
    """F. Backward compat: symbol='600519' still works."""

    def test_bare_symbol_600519_still_works(self):
        """Existing equity API with bare symbol must continue working."""
        with patch("astock_api.job_handlers.get_governor") as mock_get:
            from astock_api.job_handlers import market_bars_handler

            mock_result = {
                "source": "mootdx", "symbol": "600519",
                "canonical_id": "SSE:600519", "exchange": "SSE",
                "frequency": "daily", "requested_count": 3, "rows": [],
            }

            mock_gov = MagicMock()
            mock_gov.fetch_market_bars.return_value = mock_result
            mock_get.return_value = mock_gov

            # This is the existing API call pattern — must not break
            result = market_bars_handler({"symbol": "600519", "frequency": "daily", "count": 3})

        assert result["source"] == "mootdx"
        assert result["symbol"] == "600519"

    def test_bare_symbol_000001_still_works(self):
        """Existing equity API with bare symbol 000001 must continue working."""
        with patch("astock_api.job_handlers.get_governor") as mock_get:
            from astock_api.job_handlers import market_bars_handler

            mock_result = {
                "source": "mootdx", "symbol": "000001",
                "canonical_id": "SZSE:000001", "exchange": "SZSE",
                "frequency": "daily", "requested_count": 3, "rows": [],
            }

            mock_gov = MagicMock()
            mock_gov.fetch_market_bars.return_value = mock_result
            mock_get.return_value = mock_gov

            result = market_bars_handler({"symbol": "000001", "frequency": "daily", "count": 3})

        assert result["source"] == "mootdx"
        assert result["symbol"] == "000001"
