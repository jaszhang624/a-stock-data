"""Source adapters: protocol and concrete implementations for market data sources.

Exceptions are source-level only — no dependency on job_engine here.
The Governor and Job Handler translate these to their own error types.
"""

from abc import ABC, abstractmethod
from typing import Any


# ── Source-level exceptions ─────────────────────────────────────────

class SourceError(Exception):
    """Base exception for all source adapter errors."""


class SourceTransientError(SourceError):
    """Temporary failure: network error, rate limit (ResultCode=403), server down."""


class SourceUnsupportedError(SourceError):
    """This source does not support the requested symbol or market."""


class SourceDataError(SourceError):
    """Malformed response, missing columns, empty data."""


# ── Protocol ────────────────────────────────────────────────────────

class MarketBarsSource(ABC):
    """Protocol for market bars data sources."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Source identifier (e.g. 'mootdx', 'baidu')."""

    @abstractmethod
    def fetch_market_bars(
        self, symbol: str, frequency: str, count: int
    ) -> dict[str, Any]:
        """Fetch daily market bars.

        Returns:
            {
                "source": "<name>",
                "symbol": str,
                "frequency": str,
                "requested_count": int,
                "rows": [{"datetime", "open", "high", "low", "close", "volume", "amount"}, ...],
            }

        Raises:
            SourceTransientError: temporary failures (network, rate limit).
            SourceUnsupportedError: symbol/market not supported by this source.
            SourceDataError: malformed response, missing columns, empty data.
        """


# ── MootdxSource ───────────────────────────────────────────────────

class MootdxSource(MarketBarsSource):
    """mootdx (通达信) source adapter."""

    @property
    def name(self) -> str:
        return "mootdx"

    def fetch_market_bars(
        self, symbol: str, frequency: str, count: int
    ) -> dict[str, Any]:
        try:
            client = self._get_client()
        except Exception as e:
            raise SourceTransientError(f"mootdx client unavailable: {e}") from e

        try:
            df = client.bars(symbol, 9, offset=count)
        except Exception as e:
            raise SourceTransientError(f"mootdx bars request failed for {symbol}: {e}") from e

        if df is None or not hasattr(df, "empty") or df.empty:
            raise SourceDataError(f"mootdx returned empty bars for {symbol}")

        columns = ["datetime", "open", "high", "low", "close", "volume", "amount"]
        missing = [c for c in columns if c not in df.columns]
        if missing:
            raise SourceDataError(
                f"mootdx bars schema missing columns: {','.join(missing)}"
            )

        rows = df.tail(count)[columns].to_dict(orient="records")

        return {
            "source": self.name,
            "symbol": symbol,
            "frequency": frequency,
            "requested_count": count,
            "rows": rows,
        }

    def _get_client(self):
        from astock_api.upstream.common import tdx_client

        return tdx_client()


# ── BaiduSource ────────────────────────────────────────────────────

class BaiduSource(MarketBarsSource):
    """Baidu股市通 source adapter.

    Volume normalization: Baidu returns 股 (individual shares),
    canonical schema uses 手 (lots of 100). So volume / 100.
    Amount is already in 元 (same as mootdx).

    Known limitations:
    - ResultCode=403 (rate limit) → SourceTransientError
    - North Exchange (8x/4x) not supported → SourceUnsupportedError
    """

    @property
    def name(self) -> str:
        return "baidu"

    def fetch_market_bars(
        self, symbol: str, frequency: str, count: int
    ) -> dict[str, Any]:
        # Baidu does not support North Exchange codes
        if symbol.startswith(('4', '8')):
            raise SourceUnsupportedError(
                f"baidu does not support North Exchange symbol: {symbol}"
            )

        raw = self._fetch_raw(symbol)

        # ResultCode check — may be int or string
        rc = str(raw.get("ResultCode", -1))
        if rc != "0":
            raise SourceTransientError(
                f"baidu returned ResultCode={rc} for {symbol}"
            )

        result = raw.get("Result")
        if not isinstance(result, dict):
            raise SourceDataError(
                f"baidu returned malformed Result for {symbol}: {type(result).__name__}"
            )

        md = result.get("newMarketData")
        if not isinstance(md, dict):
            raise SourceDataError(
                f"baidu missing newMarketData for {symbol}"
            )

        keys = md.get("keys", [])
        market_data_str = md.get("marketData", "")
        if not market_data_str:
            raise SourceDataError(
                f"baidu returned empty marketData for {symbol}"
            )

        # Parse rows: semicolon-separated, comma within
        raw_rows = [r for r in market_data_str.split(";") if r.strip()]
        if not raw_rows:
            raise SourceDataError(
                f"baidu returned no parseable rows for {symbol}"
            )

        # Build canonical rows — Baidu order is chronological (oldest first)
        parsed = []
        for rr in raw_rows:
            parts = rr.split(",")
            row_dict = dict(zip(keys, parts))
            try:
                parsed.append({
                    "datetime": row_dict.get("time", ""),
                    "open": float(row_dict.get("open", 0)),
                    "high": float(row_dict.get("high", 0)),
                    "low": float(row_dict.get("low", 0)),
                    "close": float(row_dict.get("close", 0)),
                    # Baidu volume = 股 → canonical = 手 (÷100)
                    "volume": float(row_dict.get("volume", 0)) / 100,
                    # Baidu amount = 元 (same as mootdx)
                    "amount": float(row_dict.get("amount", 0)),
                })
            except (ValueError, TypeError):
                continue

        if not parsed:
            raise SourceDataError(
                f"baidu returned unparseable data for {symbol}"
            )

        # Baidu returns oldest-first; take last `count` rows
        rows = parsed[-count:] if len(parsed) > count else parsed

        return {
            "source": self.name,
            "symbol": symbol,
            "frequency": frequency,
            "requested_count": count,
            "rows": rows,
        }

    def _fetch_raw(self, symbol: str) -> dict:
        """Call baidu_kline_with_ma and return the raw response.

        Handles the case where ResultCode != 0 returns a list instead of dict.
        """
        from astock_api.upstream.tencent import baidu_kline_with_ma as _baidu

        try:
            result = _baidu(symbol)
        except Exception as e:
            raise SourceTransientError(f"baidu request failed for {symbol}: {e}") from e

        # baidu_kline_with_ma returns {'keys': ..., 'rows': ...} on success
        # but when ResultCode != 0, the upstream function may return a list
        if isinstance(result, dict):
            # Check if this is actually the raw baidu response (ResultCode present)
            if "ResultCode" in result:
                return result
            # Normal success path from baidu_kline_with_ma
            keys = result.get("keys", [])
            rows = result.get("rows", [])
            if not keys or not rows:
                raise SourceDataError(f"baidu returned empty data for {symbol}")
            # Reconstruct a pseudo-raw response for consistent parsing
            return {
                "ResultCode": 0,
                "Result": {
                    "newMarketData": {
                        "keys": keys,
                        "marketData": ";".join(rows),
                    }
                },
            }

        # Result is a list — this means ResultCode != 0 at the HTTP level
        raise SourceTransientError(
            f"baidu returned non-dict response for {symbol}: {type(result).__name__}"
        )
