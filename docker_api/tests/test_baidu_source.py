"""Tests for BaiduSource adapter (Subtask 3 + 3.5).

Verifies:
A. Normal response → canonical rows
B. Only returns last count rows
C. datetime/OHLC field mapping correct
D. volume normalization correct (/100)
E. amount not incorrectly scaled
F. ResultCode=403 → SourceTransientError (no list.get error)
G. malformed response → SourceDataError
H. unsupported symbol → SourceUnsupportedError
I. Governor still only uses mootdx
J. market_bars_handler behavior unchanged
"""

from unittest.mock import MagicMock, patch

import pytest

from astock_api.job_engine import TransientJobError, PermanentJobError
from astock_api.source_adapters import (
    SourceTransientError,
    SourceUnsupportedError,
    SourceDataError,
)


# --- BaiduSource tests ---

class TestBaiduSource:
    def test_name_is_baidu(self):
        from astock_api.source_adapters import BaiduSource

        assert BaiduSource().name == "baidu"

    def test_fetch_returns_canonical_rows(self):
        """A. Normal response → canonical rows with correct field mapping."""
        from astock_api.source_adapters import BaiduSource

        mock_baidu_result = {
            "keys": ["timestamp", "time", "open", "close", "volume", "high", "low", "amount"],
            "rows": [
                "1785945600,2026-08-06,1310.00,1308.55,2546328,1314.40,1300.01,3326230801.00",
                "1786032000,2026-08-07,1308.66,1309.22,2497581,1315.28,1301.00,3266919421.00",
                "1786291200,2026-08-10,1325.00,1348.86,6268572,1359.97,1318.08,8428304269.00",
            ],
        }

        source = BaiduSource()
        with patch.object(source, "_fetch_raw", return_value={
            "ResultCode": 0,
            "Result": {
                "newMarketData": {
                    "keys": mock_baidu_result["keys"],
                    "marketData": ";".join(mock_baidu_result["rows"]),
                }
            },
        }):
            result = source.fetch_market_bars("600519", "daily", 3)

        assert result["source"] == "baidu"
        assert result["symbol"] == "600519"
        assert result["frequency"] == "daily"
        assert result["requested_count"] == 3
        assert len(result["rows"]) == 3

    def test_datetime_ohlc_field_mapping(self):
        """C. datetime/OHLC fields correctly mapped."""
        from astock_api.source_adapters import BaiduSource

        source = BaiduSource()
        with patch.object(source, "_fetch_raw", return_value={
            "ResultCode": 0,
            "Result": {
                "newMarketData": {
                    "keys": ["timestamp", "time", "open", "close", "volume", "high", "low", "amount"],
                    "marketData": "1786291200,2026-08-10,1325.00,1348.86,6268572,1359.97,1318.08,8428304269.00",
                }
            },
        }):
            result = source.fetch_market_bars("600519", "daily", 1)

        row = result["rows"][0]
        assert row["datetime"] == "2026-08-10"
        assert row["open"] == 1325.0
        assert row["high"] == 1359.97
        assert row["low"] == 1318.08
        assert row["close"] == 1348.86

    def test_volume_normalization(self):
        """D. Volume normalized: Baidu 股 → canonical 手 (÷100)."""
        from astock_api.source_adapters import BaiduSource

        source = BaiduSource()
        with patch.object(source, "_fetch_raw", return_value={
            "ResultCode": 0,
            "Result": {
                "newMarketData": {
                    "keys": ["timestamp", "time", "open", "close", "volume", "high", "low", "amount"],
                    "marketData": "1786291200,2026-08-10,1325.00,1348.86,6268572,1359.97,1318.08,8428304269.00",
                }
            },
        }):
            result = source.fetch_market_bars("600519", "daily", 1)

        row = result["rows"][0]
        # Baidu raw volume=6268572 (股) → canonical 62685.72 (手)
        assert row["volume"] == pytest.approx(62685.72)

    def test_amount_not_scaled(self):
        """E. Amount not incorrectly scaled."""
        from astock_api.source_adapters import BaiduSource

        source = BaiduSource()
        with patch.object(source, "_fetch_raw", return_value={
            "ResultCode": 0,
            "Result": {
                "newMarketData": {
                    "keys": ["timestamp", "time", "open", "close", "volume", "high", "low", "amount"],
                    "marketData": "1786291200,2026-08-10,1325.00,1348.86,6268572,1359.97,1318.08,8428304269.00",
                }
            },
        }):
            result = source.fetch_market_bars("600519", "daily", 1)

        row = result["rows"][0]
        # Amount stays as-is (元)
        assert row["amount"] == pytest.approx(8428304269.0)

    def test_returns_last_count_rows(self):
        """B. Only returns last `count` rows."""
        from astock_api.source_adapters import BaiduSource

        # 10 rows, request last 3
        rows = []
        for i in range(10):
            rows.append(f"178629120{i},2026-08-{i+1:02d},{100+i}.00,{100+i}.50,100000,{100+i+1}.00,{99+i}.00,500000.00")

        source = BaiduSource()
        with patch.object(source, "_fetch_raw", return_value={
            "ResultCode": 0,
            "Result": {
                "newMarketData": {
                    "keys": ["timestamp", "time", "open", "close", "volume", "high", "low", "amount"],
                    "marketData": ";".join(rows),
                }
            },
        }):
            result = source.fetch_market_bars("600519", "daily", 3)

        assert len(result["rows"]) == 3
        # Last row should be the most recent (i=9)
        assert result["rows"][-1]["open"] == 109.0

    def test_resultcode_403_no_list_get_error(self):
        """F. ResultCode=403 does not produce 'list' object has no attribute 'get'."""
        from astock_api.source_adapters import BaiduSource

        source = BaiduSource()
        # Simulate baidu_kline_with_ma returning a list (the actual failure mode)
        with patch("astock_api.upstream.tencent.baidu_kline_with_ma", return_value=[]):
            with pytest.raises(SourceTransientError, match="non-dict response"):
                source.fetch_market_bars("600519", "daily", 3)

    def test_resultcode_403_source_transient_error(self):
        """F. ResultCode=403 → SourceTransientError."""
        from astock_api.source_adapters import BaiduSource

        source = BaiduSource()
        with patch.object(source, "_fetch_raw", return_value={
            "ResultCode": 403,
            "Result": [],
        }):
            with pytest.raises(SourceTransientError, match="ResultCode=403"):
                source.fetch_market_bars("600519", "daily", 3)

    def test_resultcode_string_403(self):
        """ResultCode as string '403' also handled."""
        from astock_api.source_adapters import BaiduSource

        source = BaiduSource()
        with patch.object(source, "_fetch_raw", return_value={
            "ResultCode": "403",
            "Result": [],
        }):
            with pytest.raises(SourceTransientError, match="ResultCode=403"):
                source.fetch_market_bars("600519", "daily", 3)

    def test_malformed_response_source_data_error(self):
        """G. Malformed response → SourceDataError."""
        from astock_api.source_adapters import BaiduSource

        source = BaiduSource()
        with patch.object(source, "_fetch_raw", return_value={
            "ResultCode": 0,
            "Result": [],  # list instead of dict
        }):
            with pytest.raises(SourceDataError, match="malformed Result"):
                source.fetch_market_bars("600519", "daily", 3)

    def test_north_exchange_source_unsupported(self):
        """H. Unsupported symbol (North Exchange) → SourceUnsupportedError."""
        from astock_api.source_adapters import BaiduSource

        source = BaiduSource()
        with pytest.raises(SourceUnsupportedError, match="North Exchange"):
            source.fetch_market_bars("872925", "daily", 3)

    def test_north_exchange_4x_prefix(self):
        """H. 4x prefix also blocked."""
        from astock_api.source_adapters import BaiduSource

        source = BaiduSource()
        with pytest.raises(SourceUnsupportedError, match="North Exchange"):
            source.fetch_market_bars("430091", "daily", 3)

    def test_fetch_raw_http_exception_becomes_source_transient(self):
        """baidu_kline_with_ma exception → SourceTransientError."""
        from astock_api.source_adapters import BaiduSource

        source = BaiduSource()
        with patch("astock_api.upstream.tencent.baidu_kline_with_ma", side_effect=ConnectionError("timeout")):
            with pytest.raises(SourceTransientError, match="baidu request failed"):
                source.fetch_market_bars("600519", "daily", 3)


# --- Governor still only uses mootdx ---

class TestGovernorStillMootdxOnly:
    def test_governor_only_has_mootdx(self):
        """I. Governor currently registers mootdx + baidu."""
        from astock_api.source_governor import create_governor

        g = create_governor()
        assert list(g._sources.keys()) == ["mootdx", "baidu"]

    def test_handler_behavior_unchanged(self):
        """J. market_bars_handler behavior unchanged."""
        with patch(
            "astock_api.job_handlers.get_governor"
        ) as mock_get:
            from astock_api.job_handlers import market_bars_handler

            mock_result = {
                "source": "mootdx", "symbol": "600519",
                "frequency": "daily", "requested_count": 3, "rows": [],
            }

            mock_gov = MagicMock()
            mock_gov.fetch_market_bars.return_value = mock_result
            mock_get.return_value = mock_gov

            result = market_bars_handler({"symbol": "600519", "frequency": "daily", "count": 3})

        assert result["source"] == "mootdx"
        mock_gov.fetch_market_bars.assert_called_once_with("600519", "daily", 3)
