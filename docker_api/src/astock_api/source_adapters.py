"""Source adapters: protocol and concrete implementations for market data sources.

Exceptions are source-level only — no dependency on job_engine here.
The Governor and Job Handler translate these to their own error types.

C4B-2: MarketBarsSource protocol now accepts Instrument objects for
structured identity routing. Adapters convert Instrument.exchange/code
to provider-specific format at the adapter boundary.
"""

from abc import ABC, abstractmethod
from typing import Any


# ── Source-level exceptions ─────────────────────────────────────────

class SourceError(Exception):
    """Base exception for all source adapter errors."""

    def __init__(self, message: str, source_name: str = ""):
        super().__init__(message)
        self.source_name = source_name


class SourceTransientError(SourceError):
    """Temporary failure: network error, rate limit (ResultCode=403), server down."""


class SourceUnsupportedError(SourceError):
    """This source does not support the requested symbol or market."""


class SourceDataError(SourceError):
    """Malformed response, missing columns, empty data.

    Args:
        message: Error description.
        definitive: If True, the source gave a definitive answer (e.g., empty data).
                   If False, the error might be transient (e.g., malformed response).
    """

    def __init__(self, message: str, definitive: bool = True, source_name: str = ""):
        super().__init__(message, source_name=source_name)
        self.definitive = definitive


# ── Protocol ────────────────────────────────────────────────────────

class MarketBarsSource(ABC):
    """Protocol for market bars data sources.

    C4B-2: fetch_market_bars now accepts Instrument for structured identity.
    Adapters convert Instrument.exchange/code to provider-specific format.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Source identifier (e.g. 'mootdx', 'baidu')."""

    @abstractmethod
    def fetch_market_bars(
        self, instrument: Any, frequency: str, count: int
    ) -> dict[str, Any]:
        """Fetch daily market bars.

        Args:
            instrument: Instrument object with exchange, code, asset_type.
                The adapter converts this to provider-specific format.
            frequency: 'daily' (only supported value).
            count: Number of bars to return.

        Returns:
            {
                "source": "<name>",
                "symbol": str,          # bare code (backward compat)
                "canonical_id": str,    # e.g., "SSE:600519"
                "exchange": str,        # e.g., "SSE"
                "frequency": str,
                "requested_count": int,
                "rows": [{"datetime", "open", ...}, ...],
            }

        Raises:
            SourceTransientError: temporary failures (network, rate limit).
            SourceUnsupportedError: symbol/market not supported by this source.
            SourceDataError: malformed response, missing columns, empty data.
        """


# ── MootdxSource ───────────────────────────────────────────────────

class MootdxSource(MarketBarsSource):
    """mootdx (通达信) source adapter.

    C4B-2: Uses Instrument.exchange for explicit TDX market routing.
    SSE → market=1, SZSE → market=0. BSE unsupported.
    """

    @property
    def name(self) -> str:
        return "mootdx"

    def fetch_market_bars(
        self, instrument: Any, frequency: str, count: int
    ) -> dict[str, Any]:
        from astock_api.instrument import instrument_to_mootdx_market

        # North Exchange (4x/8x) — mootdx does not support these markets.
        if instrument.code.startswith(("4", "8")):
            raise SourceUnsupportedError(
                f"mootdx does not support North Exchange symbol: {instrument.code}"
            )

        # Convert Instrument to mootdx market code
        tdx_market = instrument_to_mootdx_market(instrument)
        if tdx_market is None:
            raise SourceUnsupportedError(
                f"mootdx does not support exchange {instrument.exchange} "
                f"(code={instrument.code})"
            )

        try:
            client = self._get_client()
        except Exception as e:
            raise SourceTransientError(f"mootdx client unavailable: {e}") from e

        try:
            df = client.bars(instrument.code, 9, offset=count)
        except Exception as e:
            raise SourceTransientError(
                f"mootdx bars request failed for {instrument.code}: {e}"
            ) from e

        if df is None or not hasattr(df, "empty") or df.empty:
            raise SourceDataError(f"mootdx returned empty bars for {instrument.code}")

        columns = ["datetime", "open", "high", "low", "close", "volume", "amount"]
        missing = [c for c in columns if c not in df.columns]
        if missing:
            raise SourceDataError(
                f"mootdx bars schema missing columns: {','.join(missing)}",
                definitive=False  # Malformed response might be transient
            )

        rows = df.tail(count)[columns].to_dict(orient="records")

        return {
            "source": self.name,
            "symbol": instrument.code,
            "canonical_id": instrument.canonical_id,
            "exchange": instrument.exchange,
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

    C4B-2: Uses Instrument.exchange for provider-specific symbol format.
    SSE → sh600519, SZSE → sz000001.

    Known limitations:
    - ResultCode=403 (rate limit) → SourceTransientError
    - North Exchange (8x/4x) not supported → SourceUnsupportedError
    """

    @property
    def name(self) -> str:
        return "baidu"

    def fetch_market_bars(
        self, instrument: Any, frequency: str, count: int
    ) -> dict[str, Any]:
        from astock_api.instrument import instrument_to_baidu_symbol

        # Baidu does not support North Exchange codes
        if instrument.code.startswith(('4', '8')):
            raise SourceUnsupportedError(
                f"baidu does not support North Exchange symbol: {instrument.code}"
            )

        # Convert Instrument to Baidu provider-specific format
        baidu_symbol = instrument_to_baidu_symbol(instrument)
        if baidu_symbol is None:
            raise SourceUnsupportedError(
                f"baidu does not support exchange {instrument.exchange} "
                f"(code={instrument.code})"
            )

        raw = self._fetch_raw(baidu_symbol)

        # ResultCode check — may be int or string
        rc = str(raw.get("ResultCode", -1))
        if rc != "0":
            raise SourceTransientError(
                f"baidu returned ResultCode={rc} for {instrument.code}"
            )

        result = raw.get("Result")
        if not isinstance(result, dict):
            raise SourceDataError(
                f"baidu returned malformed Result for {instrument.code}: {type(result).__name__}",
                definitive=False  # Malformed response might be transient
            )

        md = result.get("newMarketData")
        if not isinstance(md, dict):
            raise SourceDataError(
                f"baidu missing newMarketData for {instrument.code}",
                definitive=False  # Malformed response might be transient
            )

        keys = md.get("keys", [])
        market_data_str = md.get("marketData", "")
        if not market_data_str:
            raise SourceDataError(
                f"baidu returned empty marketData for {instrument.code}"
            )

        # Parse rows: semicolon-separated, comma within
        raw_rows = [r for r in market_data_str.split(";") if r.strip()]
        if not raw_rows:
            raise SourceDataError(
                f"baidu returned no parseable rows for {instrument.code}"
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
                f"baidu returned unparseable data for {instrument.code}"
            )

        # Baidu returns oldest-first; take last `count` rows
        rows = parsed[-count:] if len(parsed) > count else parsed

        return {
            "source": self.name,
            "symbol": instrument.code,
            "canonical_id": instrument.canonical_id,
            "exchange": instrument.exchange,
            "frequency": frequency,
            "requested_count": count,
            "rows": rows,
        }

    def _fetch_raw(self, baidu_symbol: str) -> dict:
        """Call baidu_kline_with_ma and return the raw response.

        Handles the case where ResultCode != 0 returns a list instead of dict.
        """
        from astock_api.upstream.tencent import baidu_kline_with_ma as _baidu

        try:
            result = _baidu(baidu_symbol)
        except Exception as e:
            raise SourceTransientError(f"baidu request failed for {baidu_symbol}: {e}") from e

        # baidu_kline_with_ma returns {'keys': ..., 'rows': ...} on success
        # when ResultCode != 0, it returns {'keys': [], 'rows': [], 'ResultCode': ...}
        if isinstance(result, dict):
            # Check for error response from baidu_kline_with_ma
            if "ResultCode" in result:
                rc = str(result.get("ResultCode", -1))
                raise SourceTransientError(
                    f"baidu returned ResultCode={rc} for {baidu_symbol}"
                )
            # Normal success path from baidu_kline_with_ma
            keys = result.get("keys", [])
            rows = result.get("rows", [])
            if not keys or not rows:
                raise SourceDataError(f"baidu returned empty data for {baidu_symbol}")
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
            f"baidu returned non-dict response for {baidu_symbol}: {type(result).__name__}"
        )
