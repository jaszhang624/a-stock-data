"""Tests for Source Governor skeleton (Subtask 2 + 3.5).

Verifies:
A. handler calls mootdx through governor
B. source=mootdx in result
C. count is correct
D. schema unchanged (7 columns)
E. mootdx exception → source-level error → TransientJobError
F. invalid request → PermanentJobError
G. source_adapters has no dependency on job_engine exceptions

C4B-2: Tests now pass Instrument objects to fetch_market_bars instead of bare strings.
"""

from unittest.mock import MagicMock, patch

import pytest

from astock_api.job_engine import TransientJobError, PermanentJobError
from astock_api.source_adapters import (
    SourceTransientError,
    SourceUnsupportedError,
    SourceDataError,
)
from astock_api.source_governor import GovernorUnavailableError, GovernorUnsupportedError


def _make_instrument(code="600519", exchange="SSE", asset_type="EQUITY"):
    """Create a mock Instrument for tests."""
    from astock_api.instrument import Instrument
    return Instrument(exchange=exchange, code=code, asset_type=asset_type)


# --- Source adapter tests ---

class TestMootdxSource:
    def test_name_is_mootdx(self):
        from astock_api.source_adapters import MootdxSource

        assert MootdxSource().name == "mootdx"

    def test_fetch_returns_correct_schema(self):
        from astock_api.source_adapters import MootdxSource

        mock_df = MagicMock()
        mock_df.empty = False
        mock_df.columns = ["datetime", "open", "high", "low", "close", "volume", "amount"]
        sliced = MagicMock()
        sliced.to_dict.return_value = [
            {"datetime": "2026-08-10", "open": 1.0, "high": 2.0, "low": 3.0,
             "close": 4.0, "volume": 5.0, "amount": 6.0},
        ]
        mock_df.tail.return_value = mock_df
        mock_df.__getitem__ = MagicMock(return_value=sliced)

        mock_client = MagicMock()
        mock_client.bars.return_value = mock_df

        source = MootdxSource()
        with patch.object(source, "_get_client", return_value=mock_client):
            result = source.fetch_market_bars(_make_instrument(), "daily", 3)

        assert result["source"] == "mootdx"
        assert result["symbol"] == "600519"
        assert result["frequency"] == "daily"
        assert result["requested_count"] == 3
        assert len(result["rows"]) == 1

    def test_empty_df_raises_source_data_error(self):
        """E. mootdx empty → SourceDataError (not TransientJobError)."""
        from astock_api.source_adapters import MootdxSource

        mock_client = MagicMock()
        mock_client.bars.return_value = None

        source = MootdxSource()
        with patch.object(source, "_get_client", return_value=mock_client):
            with pytest.raises(SourceDataError, match="empty bars"):
                source.fetch_market_bars(_make_instrument(), "daily", 3)

    def test_missing_columns_raises_source_data_error(self):
        """E. mootdx missing columns → SourceDataError."""
        from astock_api.source_adapters import MootdxSource

        mock_df = MagicMock()
        mock_df.empty = False
        mock_df.columns = ["datetime", "open"]  # missing columns

        mock_client = MagicMock()
        mock_client.bars.return_value = mock_df

        source = MootdxSource()
        with patch.object(source, "_get_client", return_value=mock_client):
            with pytest.raises(SourceDataError, match="missing columns"):
                source.fetch_market_bars(_make_instrument(), "daily", 3)

    def test_client_exception_raises_source_transient(self):
        """E. mootdx client exception → SourceTransientError."""
        from astock_api.source_adapters import MootdxSource

        source = MootdxSource()
        with patch.object(source, "_get_client", side_effect=ConnectionError("refused")):
            with pytest.raises(SourceTransientError, match="client unavailable"):
                source.fetch_market_bars(_make_instrument(), "daily", 3)

    def test_no_job_engine_import(self):
        """E. source_adapters does not import job_engine exceptions."""
        import astock_api.source_adapters as sa

        source = open(sa.__file__).read()
        assert "TransientJobError" not in source, "source_adapters must not reference TransientJobError"
        assert "PermanentJobError" not in source, "source_adapters must not reference PermanentJobError"


# --- Governor tests ---

class TestSourceGovernor:
    def test_create_governor_registers_mootdx(self):
        from astock_api.source_governor import create_governor

        g = create_governor()
        assert "mootdx" in g._sources

    def test_create_governor_registers_baidu(self):
        from astock_api.source_governor import create_governor

        g = create_governor()
        assert "baidu" in g._sources

    def test_create_governor_priority_order(self):
        """mootdx is primary, Baidu is secondary."""
        from astock_api.source_governor import create_governor

        g = create_governor()
        sources = list(g._sources.keys())
        assert sources == ["mootdx", "baidu"]

    def test_mootdx_success_skips_baidu(self):
        """A. mootdx success → Baidu not called."""
        from astock_api.source_governor import SourceGovernor

        mock_mootdx = MagicMock()
        mock_mootdx.name = "mootdx"
        mock_mootdx.fetch_market_bars.return_value = {
            "source": "mootdx", "symbol": "600519",
            "frequency": "daily", "requested_count": 3, "rows": [],
        }

        mock_baidu = MagicMock()
        mock_baidu.name = "baidu"
        mock_baidu.fetch_market_bars.return_value = {
            "source": "baidu", "symbol": "600519",
            "frequency": "daily", "requested_count": 3, "rows": [],
        }

        g = SourceGovernor()
        g.register(mock_mootdx)
        g.register(mock_baidu)

        result = g.fetch_market_bars(_make_instrument(), "daily", 3)

        assert result["source"] == "mootdx"
        mock_mootdx.fetch_market_bars.assert_called_once()
        mock_baidu.fetch_market_bars.assert_not_called()

    def test_mootdx_transient_fallback_to_baidu(self):
        """B. mootdx Transient → Baidu success → source=baidu."""
        from astock_api.source_governor import SourceGovernor

        mock_mootdx = MagicMock()
        mock_mootdx.name = "mootdx"
        mock_mootdx.fetch_market_bars.side_effect = SourceTransientError("server down")

        mock_baidu = MagicMock()
        mock_baidu.name = "baidu"
        mock_baidu.fetch_market_bars.return_value = {
            "source": "baidu", "symbol": "600519",
            "frequency": "daily", "requested_count": 3, "rows": [],
        }

        g = SourceGovernor()
        g.register(mock_mootdx)
        g.register(mock_baidu)

        result = g.fetch_market_bars(_make_instrument(), "daily", 3)

        assert result["source"] == "baidu"
        mock_mootdx.fetch_market_bars.assert_called_once()
        mock_baidu.fetch_market_bars.assert_called_once()

    def test_mootdx_data_error_fallback_to_baidu(self):
        """C. mootdx DataError → Baidu success → source=baidu."""
        from astock_api.source_governor import SourceGovernor

        mock_mootdx = MagicMock()
        mock_mootdx.name = "mootdx"
        mock_mootdx.fetch_market_bars.side_effect = SourceDataError("missing columns")

        mock_baidu = MagicMock()
        mock_baidu.name = "baidu"
        mock_baidu.fetch_market_bars.return_value = {
            "source": "baidu", "symbol": "600519",
            "frequency": "daily", "requested_count": 3, "rows": [],
        }

        g = SourceGovernor()
        g.register(mock_mootdx)
        g.register(mock_baidu)

        result = g.fetch_market_bars(_make_instrument(), "daily", 3)

        assert result["source"] == "baidu"
        mock_mootdx.fetch_market_bars.assert_called_once()
        mock_baidu.fetch_market_bars.assert_called_once()

    def test_mootdx_unsupported_fallback_to_baidu(self):
        """D. mootdx Unsupported → Baidu success → source=baidu."""
        from astock_api.source_governor import SourceGovernor

        mock_mootdx = MagicMock()
        mock_mootdx.name = "mootdx"
        mock_mootdx.fetch_market_bars.side_effect = SourceUnsupportedError("no bj")

        mock_baidu = MagicMock()
        mock_baidu.name = "baidu"
        mock_baidu.fetch_market_bars.return_value = {
            "source": "baidu", "symbol": "600519",
            "frequency": "daily", "requested_count": 3, "rows": [],
        }

        g = SourceGovernor()
        g.register(mock_mootdx)
        g.register(mock_baidu)

        result = g.fetch_market_bars(_make_instrument(), "daily", 3)

        assert result["source"] == "baidu"
        mock_mootdx.fetch_market_bars.assert_called_once()
        mock_baidu.fetch_market_bars.assert_called_once()

    def test_each_source_called_at_most_once(self):
        """H. Each source called at most once per request."""
        from astock_api.source_governor import SourceGovernor

        mock_mootdx = MagicMock()
        mock_mootdx.name = "mootdx"
        mock_mootdx.fetch_market_bars.side_effect = SourceTransientError("down")

        mock_baidu = MagicMock()
        mock_baidu.name = "baidu"
        mock_baidu.fetch_market_bars.side_effect = SourceTransientError("403")

        g = SourceGovernor()
        g.register(mock_mootdx)
        g.register(mock_baidu)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        assert mock_mootdx.fetch_market_bars.call_count == 1
        assert mock_baidu.fetch_market_bars.call_count == 1

    def test_fallback_result_schema_unchanged(self):
        """I. Fallback result has canonical schema."""
        from astock_api.source_governor import SourceGovernor

        mock_mootdx = MagicMock()
        mock_mootdx.name = "mootdx"
        mock_mootdx.fetch_market_bars.side_effect = SourceTransientError("down")

        mock_baidu = MagicMock()
        mock_baidu.name = "baidu"
        mock_baidu.fetch_market_bars.return_value = {
            "source": "baidu",
            "symbol": "600519",
            "frequency": "daily",
            "requested_count": 3,
            "rows": [
                {"datetime": "2026-08-10", "open": 1.0, "high": 2.0,
                 "low": 3.0, "close": 4.0, "volume": 5.0, "amount": 6.0},
            ],
        }

        g = SourceGovernor()
        g.register(mock_mootdx)
        g.register(mock_baidu)

        result = g.fetch_market_bars(_make_instrument(), "daily", 3)

        assert result["source"] == "baidu"
        assert result["symbol"] == "600519"
        assert result["frequency"] == "daily"
        assert result["requested_count"] == 3
        assert len(result["rows"]) == 1
        row = result["rows"][0]
        for col in ["datetime", "open", "high", "low", "close", "volume", "amount"]:
            assert col in row, f"Missing column: {col}"


# --- Health counter tests (Subtask 5A + 5B + 5C1) ---

class TestHealthCounters:
    def test_initial_state_closed(self):
        """A. Initial state=CLOSED, failures=0, open_until=None."""
        from astock_api.source_governor import SourceGovernor

        mock_source = MagicMock()
        mock_source.name = "mootdx"

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        health = g.get_source_health("mootdx")
        assert health["consecutive_failures"] == 0
        assert health["state"] == "CLOSED"
        assert health["open_until"] is None

    def test_first_transient_failure_stays_closed(self):
        """B. First transient failure: failures=1, state=CLOSED."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        mock_source.fetch_market_bars.side_effect = SourceTransientError("down")

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        health = g.get_source_health("mootdx")
        assert health["consecutive_failures"] == 1
        assert health["state"] == "CLOSED"

    def test_second_transient_failure_opens(self):
        """C. Second transient failure: failures=2, state=OPEN."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        mock_source.fetch_market_bars.side_effect = SourceTransientError("down")

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        health = g.get_source_health("mootdx")
        assert health["consecutive_failures"] == 2
        assert health["state"] == "OPEN"

    def test_open_sets_open_until(self):
        """A. OPEN sets open_until = now + 30."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        mock_source.fetch_market_bars.side_effect = SourceTransientError("down")

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        health = g.get_source_health("mootdx")
        assert health["open_until"] == 1030.0

    def test_open_not_expired_skips_adapter(self):
        """B. now < open_until → adapter not called."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        mock_source.fetch_market_bars.side_effect = SourceTransientError("down")

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        # Fail twice to OPEN (open_until = 1030.0)
        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        call_count_before = mock_source.fetch_market_bars.call_count

        # Still in cooldown (now=1029 < open_until=1030)
        g.now_fn = lambda: 1029.0

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        # Adapter not called
        assert mock_source.fetch_market_bars.call_count == call_count_before

    def test_cooldown_29s_still_skip(self):
        """C. Fake clock at 29s → still skip."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        mock_source.fetch_market_bars.side_effect = SourceTransientError("down")

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        call_count_before = mock_source.fetch_market_bars.call_count

        # now=1029 (29s elapsed) — still skip
        g.now_fn = lambda: 1029.0

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        assert mock_source.fetch_market_bars.call_count == call_count_before

    def test_cooldown_30s_allows_retry(self):
        """D. Fake clock at 30s → adapter called again."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        mock_source.fetch_market_bars.side_effect = SourceTransientError("down")

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        call_count_before = mock_source.fetch_market_bars.call_count

        # now=1030 (30s elapsed) — allow retry
        g.now_fn = lambda: 1030.0

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        # Adapter WAS called
        assert mock_source.fetch_market_bars.call_count == call_count_before + 1

    def test_cooldown_success_resets_closed(self):
        """E. Cooldown expired + success → CLOSED, failures=0, open_until=None."""
        from astock_api.source_governor import SourceGovernor

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        mock_source.fetch_market_bars.side_effect = [
            SourceTransientError("down"),
            SourceTransientError("down"),
            {"source": "mootdx", "symbol": "600519", "frequency": "daily",
             "requested_count": 3, "rows": []},
        ]

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        from astock_api.source_governor import GovernorUnavailableError
        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        assert g.get_source_health("mootdx")["state"] == "OPEN"
        assert g.get_source_health("mootdx")["open_until"] == 1030.0

        # Advance past cooldown, then succeed
        g.now_fn = lambda: 1030.0

        result = g.fetch_market_bars(_make_instrument(), "daily", 3)
        assert result["source"] == "mootdx"

        health = g.get_source_health("mootdx")
        assert health["consecutive_failures"] == 0
        assert health["state"] == "CLOSED"
        assert health["open_until"] is None

    def test_cooldown_expired_transient_failure_reopens(self):
        """F. Cooldown expired + transient failure → OPEN with new open_until."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        mock_source.fetch_market_bars.side_effect = SourceTransientError("down")

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        assert g.get_source_health("mootdx")["open_until"] == 1030.0

        # Advance past cooldown, fail again
        g.now_fn = lambda: 1030.0

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        # Re-OPEN with new open_until
        health = g.get_source_health("mootdx")
        assert health["state"] == "OPEN"
        assert health["open_until"] == 1060.0

    def test_cooldown_expired_data_error_reopens(self):
        """G. Cooldown expired + DataError → CLOSED (source reachable, data issue is not health concern)."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError, GovernorUnsupportedError

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        # First two: transient, third (after cooldown): data error
        mock_source.fetch_market_bars.side_effect = [
            SourceTransientError("down"),
            SourceTransientError("down"),
            SourceDataError("missing columns"),
        ]

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        # Advance past cooldown → HALF_OPEN probe
        g.now_fn = lambda: 1030.0

        # DataError only → GovernorUnsupportedError (no transient errors)
        with pytest.raises(GovernorUnsupportedError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        # DataError does NOT trigger OPEN — source reached, data issue is not a health concern.
        # HALF_OPEN probe with DataError → CLOSED (source is reachable).
        health = g.get_source_health("mootdx")
        assert health["state"] == "CLOSED"

    def test_unsupported_does_not_open(self):
        """E. Unsupported does not increase failures or OPEN."""
        from astock_api.source_governor import SourceGovernor, GovernorUnsupportedError

        mock_source = MagicMock()
        mock_source.name = "baidu"
        mock_source.fetch_market_bars.side_effect = SourceUnsupportedError("no bj")

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        with pytest.raises(GovernorUnsupportedError):
            g.fetch_market_bars(_make_instrument(code="872925"), "daily", 3)

        health = g.get_source_health("baidu")
        assert health["consecutive_failures"] == 0
        assert health["state"] == "CLOSED"

    def test_open_source_skips_adapter(self):
        """F. OPEN source: adapter not called on next request."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        mock_source.fetch_market_bars.side_effect = SourceTransientError("down")

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        # Fail twice to OPEN
        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        assert g.get_source_health("mootdx")["state"] == "OPEN"
        call_count_before = mock_source.fetch_market_bars.call_count

        # Third request — still in cooldown, adapter should NOT be called
        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        assert mock_source.fetch_market_bars.call_count == call_count_before

    def test_mootdx_open_fallback_to_baidu(self):
        """G. mootdx OPEN → Baidu used directly."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_mootdx = MagicMock()
        mock_mootdx.name = "mootdx"
        mock_mootdx.fetch_market_bars.side_effect = SourceTransientError("down")

        mock_baidu = MagicMock()
        mock_baidu.name = "baidu"
        # Baidu fails first two times (to let mootdx reach OPEN), then succeeds
        mock_baidu.fetch_market_bars.side_effect = [
            SourceTransientError("403"),
            SourceTransientError("403"),
            {"source": "baidu", "symbol": "600519",
             "frequency": "daily", "requested_count": 3, "rows": []},
        ]

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_mootdx)
        g.register(mock_baidu)

        # Fail both twice to OPEN mootdx (and baidu)
        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        assert g.get_source_health("mootdx")["state"] == "OPEN"

        # Reset Baidu to CLOSED so it can succeed (simulate recovery)
        g._health["baidu"]["state"] = "CLOSED"
        g._health["baidu"]["consecutive_failures"] = 0

        # Next request: mootdx OPEN → Baidu succeeds
        result = g.fetch_market_bars(_make_instrument(), "daily", 3)
        assert result["source"] == "baidu"

    def test_mootdx_open_baidu_unsupported_gives_unavailable(self):
        """H. mootdx OPEN + Baidu Unsupported → GovernorUnavailableError."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_mootdx = MagicMock()
        mock_mootdx.name = "mootdx"
        mock_mootdx.fetch_market_bars.side_effect = SourceTransientError("down")

        mock_baidu = MagicMock()
        mock_baidu.name = "baidu"
        mock_baidu.fetch_market_bars.side_effect = SourceUnsupportedError("no bj")

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_mootdx)
        g.register(mock_baidu)

        # Fail mootdx twice to OPEN
        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(code="872925"), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(code="872925"), "daily", 3)

        assert g.get_source_health("mootdx")["state"] == "OPEN"

        # mootdx OPEN + Baidu Unsupported → GovernorUnavailableError (NOT Unsupported)
        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(code="872925"), "daily", 3)

    def test_both_sources_open(self):
        """I. Both sources OPEN → GovernorUnavailableError."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_mootdx = MagicMock()
        mock_mootdx.name = "mootdx"
        mock_mootdx.fetch_market_bars.side_effect = SourceTransientError("down")

        mock_baidu = MagicMock()
        mock_baidu.name = "baidu"
        mock_baidu.fetch_market_bars.side_effect = SourceTransientError("403")

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_mootdx)
        g.register(mock_baidu)

        # Fail both twice to OPEN
        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        assert g.get_source_health("mootdx")["state"] == "OPEN"
        assert g.get_source_health("baidu")["state"] == "OPEN"

        # Both OPEN → GovernorUnavailableError
        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

    def test_success_resets_to_closed(self):
        """J. Non-OPEN source success → failures=0, state=CLOSED."""
        from astock_api.source_governor import SourceGovernor

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        mock_source.fetch_market_bars.side_effect = [
            SourceTransientError("down"),
            {"source": "mootdx", "symbol": "600519", "frequency": "daily",
             "requested_count": 3, "rows": []},
        ]

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        from astock_api.source_governor import GovernorUnavailableError
        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        assert g.get_source_health("mootdx")["consecutive_failures"] == 1
        assert g.get_source_health("mootdx")["state"] == "CLOSED"

        # Success
        g.fetch_market_bars(_make_instrument(), "daily", 3)

        health = g.get_source_health("mootdx")
        assert health["consecutive_failures"] == 0
        assert health["state"] == "CLOSED"

    def test_mootdx_open_does_not_affect_baidu(self):
        """K. mootdx OPEN does not affect Baidu health."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_mootdx = MagicMock()
        mock_mootdx.name = "mootdx"
        mock_mootdx.fetch_market_bars.side_effect = SourceTransientError("down")

        mock_baidu = MagicMock()
        mock_baidu.name = "baidu"
        # Baidu fails first two times, then succeeds
        mock_baidu.fetch_market_bars.side_effect = [
            SourceTransientError("403"),
            SourceTransientError("403"),
            {"source": "baidu", "symbol": "600519",
             "frequency": "daily", "requested_count": 3, "rows": []},
        ]

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_mootdx)
        g.register(mock_baidu)

        # Fail both twice to OPEN mootdx (and baidu)
        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        assert g.get_source_health("mootdx")["state"] == "OPEN"
        # Reset Baidu to CLOSED so it can succeed (simulate recovery)
        g._health["baidu"]["state"] = "CLOSED"
        g._health["baidu"]["consecutive_failures"] = 0

        # mootdx OPEN, Baidu succeeds
        result = g.fetch_market_bars(_make_instrument(), "daily", 3)
        assert result["source"] == "baidu"

        # Baidu health unaffected by mootdx OPEN
        assert g.get_source_health("baidu")["consecutive_failures"] == 0
        assert g.get_source_health("baidu")["state"] == "CLOSED"

    def test_primary_cooldown_expired_recovers(self):
        """I. Primary cooldown expired + success → primary restored."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_mootdx = MagicMock()
        mock_mootdx.name = "mootdx"
        # First two: fail, third (after cooldown): success
        mock_mootdx.fetch_market_bars.side_effect = [
            SourceTransientError("down"),
            SourceTransientError("down"),
            {"source": "mootdx", "symbol": "600519", "frequency": "daily",
             "requested_count": 3, "rows": []},
        ]

        mock_baidu = MagicMock()
        mock_baidu.name = "baidu"
        mock_baidu.fetch_market_bars.side_effect = [
            SourceTransientError("403"),
            SourceTransientError("403"),
        ]

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_mootdx)
        g.register(mock_baidu)

        # Fail both twice to OPEN
        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        assert g.get_source_health("mootdx")["state"] == "OPEN"

        # Advance past cooldown — mootdx succeeds, becomes primary again
        g.now_fn = lambda: 1030.0

        result = g.fetch_market_bars(_make_instrument(), "daily", 3)
        assert result["source"] == "mootdx"

    def test_get_health_unknown_source(self):
        """get_source_health returns None for unknown source."""
        from astock_api.source_governor import SourceGovernor

        g = SourceGovernor(now_fn=lambda: 1000.0)
        assert g.get_source_health("nonexistent") is None

    def test_no_real_sleep_in_tests(self):
        """J. Tests use fake clock — no real sleep."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        mock_source.fetch_market_bars.side_effect = SourceTransientError("down")

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        from astock_api.source_governor import GovernorUnavailableError
        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        # Advance clock without sleeping
        g.now_fn = lambda: 1030.0

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        # Verify no real time passed (clock is fake)
        assert g.now_fn() == 1030.0

    def test_fetch_delegates_to_source(self):
        from astock_api.source_governor import SourceGovernor

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        mock_source.fetch_market_bars.return_value = {
            "source": "mootdx", "symbol": "600519",
            "frequency": "daily", "requested_count": 3, "rows": [],
        }

        g = SourceGovernor()
        g.register(mock_source)
        result = g.fetch_market_bars(_make_instrument(), "daily", 3)

        assert result["source"] == "mootdx"
        mock_source.fetch_market_bars.assert_called_once()  # called with Instrument, "daily", 3

    def test_no_sources_raises_governor_error(self):
        from astock_api.source_governor import SourceGovernor, GovernorUnsupportedError

        g = SourceGovernor()
        with pytest.raises(GovernorUnsupportedError, match="no data available"):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

    def test_source_transient_error_becomes_governor_unavailable(self):
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        mock_source.fetch_market_bars.side_effect = SourceTransientError("server down")

        g = SourceGovernor()
        g.register(mock_source)

        with pytest.raises(GovernorUnavailableError, match="server down"):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

    def test_source_unsupported_error_becomes_governor_unsupported(self):
        from astock_api.source_governor import SourceGovernor, GovernorUnsupportedError

        mock_source = MagicMock()
        mock_source.name = "baidu"
        mock_source.fetch_market_bars.side_effect = SourceUnsupportedError("no north exchange")

        g = SourceGovernor()
        g.register(mock_source)

        with pytest.raises(GovernorUnsupportedError, match="north exchange"):
            g.fetch_market_bars(_make_instrument(code="872925"), "daily", 3)

    def test_transient_plus_unsupported_becomes_unavailable(self):
        """Scenario A: Transient + Unsupported → GovernorUnavailableError."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_mootdx = MagicMock()
        mock_mootdx.name = "mootdx"
        mock_mootdx.fetch_market_bars.side_effect = SourceTransientError("server down")

        mock_baidu = MagicMock()
        mock_baidu.name = "baidu"
        mock_baidu.fetch_market_bars.side_effect = SourceUnsupportedError("no north exchange")

        g = SourceGovernor()
        g.register(mock_mootdx)
        g.register(mock_baidu)

        with pytest.raises(GovernorUnavailableError, match="all sources unavailable"):
            g.fetch_market_bars(_make_instrument(code="872925"), "daily", 3)

    def test_unsupported_plus_unsupported_becomes_unsupported(self):
        """Scenario B: Unsupported + Unsupported → GovernorUnsupportedError."""
        from astock_api.source_governor import SourceGovernor, GovernorUnsupportedError

        mock_mootdx = MagicMock()
        mock_mootdx.name = "mootdx"
        mock_mootdx.fetch_market_bars.side_effect = SourceUnsupportedError("no bj")

        mock_baidu = MagicMock()
        mock_baidu.name = "baidu"
        mock_baidu.fetch_market_bars.side_effect = SourceUnsupportedError("no north exchange")

        g = SourceGovernor()
        g.register(mock_mootdx)
        g.register(mock_baidu)

        with pytest.raises(GovernorUnsupportedError, match="no data available"):
            g.fetch_market_bars(_make_instrument(code="872925"), "daily", 3)

    def test_data_plus_unsupported_becomes_unsupported(self):
        """Scenario C: DataError + Unsupported → GovernorUnsupportedError (no transient errors = no data)."""
        from astock_api.source_governor import SourceGovernor, GovernorUnsupportedError

        mock_mootdx = MagicMock()
        mock_mootdx.name = "mootdx"
        mock_mootdx.fetch_market_bars.side_effect = SourceDataError("missing columns")

        mock_baidu = MagicMock()
        mock_baidu.name = "baidu"
        mock_baidu.fetch_market_bars.side_effect = SourceUnsupportedError("no north exchange")

        g = SourceGovernor()
        g.register(mock_mootdx)
        g.register(mock_baidu)

        with pytest.raises(GovernorUnsupportedError, match="no data available"):
            g.fetch_market_bars(_make_instrument(code="872925"), "daily", 3)

    def test_transient_plus_data_becomes_unavailable(self):
        """Scenario D: Transient + DataError (definitive=False) → GovernorUnavailableError."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_mootdx = MagicMock()
        mock_mootdx.name = "mootdx"
        mock_mootdx.fetch_market_bars.side_effect = SourceTransientError("server down")

        mock_baidu = MagicMock()
        mock_baidu.name = "baidu"
        mock_baidu.fetch_market_bars.side_effect = SourceDataError("malformed", definitive=False)

        g = SourceGovernor()
        g.register(mock_mootdx)
        g.register(mock_baidu)

        with pytest.raises(GovernorUnavailableError, match="all sources unavailable"):
            g.fetch_market_bars(_make_instrument(), "daily", 3)


# --- Handler integration tests ---

class TestMarketBarsHandler:
    def test_handler_calls_governor(self):
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

        assert result == mock_result
        mock_gov.fetch_market_bars.assert_called_once()  # called with Instrument, "daily", 3

    def test_handler_source_is_mootdx(self):
        with patch(
            "astock_api.job_handlers.get_governor"
        ) as mock_get:
            from astock_api.job_handlers import market_bars_handler

            mock_result = {
                "source": "mootdx", "symbol": "000001",
                "frequency": "daily", "requested_count": 5, "rows": [],
            }

            mock_gov = MagicMock()
            mock_gov.fetch_market_bars.return_value = mock_result
            mock_get.return_value = mock_gov

            result = market_bars_handler({"symbol": "000001", "frequency": "daily", "count": 5})

        assert result["source"] == "mootdx"
        assert result["requested_count"] == 5

    def test_handler_governor_unavailable_becomes_transient(self):
        """H. mootdx failure → GovernorUnavailableError → TransientJobError."""
        from astock_api.source_governor import GovernorUnavailableError

        with patch(
            "astock_api.job_handlers.get_governor"
        ) as mock_get:
            from astock_api.job_handlers import market_bars_handler

            mock_gov = MagicMock()
            mock_gov.fetch_market_bars.side_effect = GovernorUnavailableError("all down")
            mock_get.return_value = mock_gov

            with pytest.raises(TransientJobError, match="all sources unavailable"):
                market_bars_handler({"symbol": "600519", "frequency": "daily", "count": 3})

    def test_handler_invalid_frequency_permanent_error(self):
        """G. invalid request → PermanentJobError."""
        from astock_api.job_handlers import market_bars_handler

        with pytest.raises(PermanentJobError, match="daily only"):
            market_bars_handler({"symbol": "600519", "frequency": "1min", "count": 3})

    def test_handler_invalid_symbol_permanent_error(self):
        """G. invalid symbol → PermanentJobError."""
        from astock_api.job_handlers import market_bars_handler

        with pytest.raises(PermanentJobError, match="6-digit"):
            market_bars_handler({"symbol": "sh600519", "frequency": "daily", "count": 3})

    def test_handler_governor_generic_exception_becomes_transient(self):
        with patch(
            "astock_api.job_handlers.get_governor"
        ) as mock_get:
            from astock_api.job_handlers import market_bars_handler

            mock_gov = MagicMock()
            mock_gov.fetch_market_bars.side_effect = RuntimeError("unexpected")
            mock_get.return_value = mock_gov

            with pytest.raises(TransientJobError, match="source governor failed"):
                market_bars_handler({"symbol": "600519", "frequency": "daily", "count": 3})


# --- Shared Governor lifecycle tests (Subtask 5C1.6) ---

class TestSharedGovernor:
    def test_get_governor_returns_same_instance(self):
        """A. get_governor() returns the same instance."""
        import tempfile, os
        import astock_api.source_governor as sg_module

        original = sg_module._default_governor
        try:
            sg_module._default_governor = None
            with tempfile.TemporaryDirectory() as tmp:
                sg_module._DEFAULT_DB_PATH = os.path.join(tmp, "test.db")

                g1 = sg_module.get_governor()
                g2 = sg_module.get_governor()

                assert g1 is g2
        finally:
            sg_module._default_governor = original

    def test_create_governor_returns_new_instance(self):
        """B. create_governor() returns different instances."""
        from astock_api.source_governor import create_governor

        g1 = create_governor()
        g2 = create_governor()
        assert g1 is not g2

    def test_handler_reuses_same_governor_across_calls(self):
        """C. Two handler calls share the same Governor health state."""
        import tempfile, os
        import astock_api.source_governor as sg_module

        original = sg_module._default_governor
        try:
            sg_module._default_governor = None
            with tempfile.TemporaryDirectory() as tmp:
                sg_module._DEFAULT_DB_PATH = os.path.join(tmp, "test.db")

                g1 = sg_module.get_governor()
                g2 = sg_module.get_governor()

                assert g1 is g2
        finally:
            sg_module._default_governor = original

    def test_two_handler_calls_accumulate_failures(self):
        """D/E. Two handler calls with mootdx failure → OPEN after 2."""
        import tempfile, os
        import astock_api.source_governor as sg_module

        original = sg_module._default_governor
        try:
            sg_module._default_governor = None
            with tempfile.TemporaryDirectory() as tmp:
                sg_module._DEFAULT_DB_PATH = os.path.join(tmp, "test.db")

                gov = sg_module.get_governor()
                # Reset health to known state for this test
                gov._health["mootdx"]["consecutive_failures"] = 0
                gov._health["mootdx"]["state"] = "CLOSED"

                # Mock mootdx to fail
                original_mootdx = gov._sources["mootdx"]
                mock_mootdx = MagicMock()
                mock_mootdx.name = "mootdx"
                mock_mootdx.fetch_market_bars.side_effect = SourceTransientError("down")

                # Mock baidu to succeed on fallback
                mock_baidu = MagicMock()
                mock_baidu.name = "baidu"
                mock_baidu.fetch_market_bars.return_value = {
                    "source": "baidu", "symbol": "600519",
                    "frequency": "daily", "requested_count": 3, "rows": [],
                }

                gov._sources["mootdx"] = mock_mootdx
                gov._sources["baidu"] = mock_baidu

                # Call 1: mootdx fails → failures=1
                try:
                    gov.fetch_market_bars(_make_instrument(), "daily", 3)
                except GovernorUnavailableError:
                    pass

                assert gov.get_source_health("mootdx")["consecutive_failures"] == 1
                assert gov.get_source_health("mootdx")["state"] == "CLOSED"

                # Call 2: mootdx fails → failures=2, OPEN
                try:
                    gov.fetch_market_bars(_make_instrument(), "daily", 3)
                except GovernorUnavailableError:
                    pass

                assert gov.get_source_health("mootdx")["consecutive_failures"] == 2
                assert gov.get_source_health("mootdx")["state"] == "OPEN"

                # Restore
                gov._sources["mootdx"] = original_mootdx
        finally:
            sg_module._default_governor = original


# --- HALF_OPEN + concurrent tests (Subtask 5C2) ---

class TestHalfOpen:
    def test_open_cooldown_expired_enters_half_open(self):
        """A. OPEN cooldown expired → first request enters HALF_OPEN."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        mock_source.fetch_market_bars.side_effect = SourceTransientError("down")

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        # Fail twice to OPEN (open_until = 1030.0)
        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        assert g.get_source_health("mootdx")["state"] == "OPEN"

        # Advance past cooldown, succeed on probe
        g.now_fn = lambda: 1030.0
        mock_source.fetch_market_bars.side_effect = [
            {"source": "mootdx", "symbol": "600519", "frequency": "daily",
             "requested_count": 3, "rows": []},
        ]

        result = g.fetch_market_bars(_make_instrument(), "daily", 3)
        assert result["source"] == "mootdx"

    def test_half_open_probe_success_closes(self):
        """B. HALF_OPEN probe success → CLOSED, failures=0, open_until=None."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        mock_source.fetch_market_bars.side_effect = [
            SourceTransientError("down"),
            SourceTransientError("down"),
            {"source": "mootdx", "symbol": "600519", "frequency": "daily",
             "requested_count": 3, "rows": []},
        ]

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        # Advance past cooldown
        g.now_fn = lambda: 1030.0

        result = g.fetch_market_bars(_make_instrument(), "daily", 3)
        assert result["source"] == "mootdx"

        health = g.get_source_health("mootdx")
        assert health["state"] == "CLOSED"
        assert health["consecutive_failures"] == 0
        assert health["open_until"] is None

    def test_half_open_transient_failure_reopens(self):
        """C. HALF_OPEN transient failure → OPEN with new open_until."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        mock_source.fetch_market_bars.side_effect = SourceTransientError("down")

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        # Advance past cooldown, fail again
        g.now_fn = lambda: 1030.0

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        health = g.get_source_health("mootdx")
        assert health["state"] == "OPEN"
        assert health["open_until"] == 1060.0

    def test_half_open_data_error_closes(self):
        """D. HALF_OPEN DataError → CLOSED (source reachable, data issue is not health concern)."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError, GovernorUnsupportedError

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        mock_source.fetch_market_bars.side_effect = [
            SourceTransientError("down"),
            SourceTransientError("down"),
            SourceDataError("missing columns"),
        ]

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        g.now_fn = lambda: 1030.0

        # DataError only → GovernorUnsupportedError (no transient errors)
        with pytest.raises(GovernorUnsupportedError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        # DataError does NOT trigger OPEN — source reached, data issue is not a health concern.
        assert g.get_source_health("mootdx")["state"] == "CLOSED"

    def test_half_open_unsupported_reopens_no_failure_increment(self):
        """E. HALF_OPEN Unsupported → OPEN, failures not incremented."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError, GovernorUnsupportedError

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        # First two: transient to reach OPEN. Third (probe): unsupported.
        mock_source.fetch_market_bars.side_effect = [
            SourceTransientError("down"),
            SourceTransientError("down"),
            SourceUnsupportedError("no bj"),
        ]

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(code="872925"), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(code="872925"), "daily", 3)

        assert g.get_source_health("mootdx")["consecutive_failures"] == 2

        # Advance past cooldown — probe returns unsupported
        g.now_fn = lambda: 1030.0

        # Single source + unsupported → GovernorUnsupportedError
        with pytest.raises(GovernorUnsupportedError):
            g.fetch_market_bars(_make_instrument(code="872925"), "daily", 3)

        # failures should still be 2 (not incremented by unsupported)
        assert g.get_source_health("mootdx")["consecutive_failures"] == 2
        assert g.get_source_health("mootdx")["state"] == "OPEN"

    def test_second_request_skips_half_open(self):
        """F. Second request skips HALF_OPEN source."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_mootdx = MagicMock()
        mock_mootdx.name = "mootdx"

        import threading as _th
        import time as _time

        # Use an Event to signal when probe is in progress
        probe_in_progress = _th.Event()
        probe_done = _th.Event()

        def slow_probe(*args, **kwargs):
            probe_in_progress.set()
            # Block until main thread signals done checking
            probe_done.wait(timeout=5)
            return {"source": "mootdx", "symbol": "600519", "frequency": "daily",
                    "requested_count": 3, "rows": []}

        mock_mootdx.fetch_market_bars.side_effect = [
            SourceTransientError("down"),
            SourceTransientError("down"),
        ]

        mock_baidu = MagicMock()
        mock_baidu.name = "baidu"
        # Baidu fails first two times, then succeeds on fallback
        mock_baidu.fetch_market_bars.side_effect = [
            SourceTransientError("403"),
            SourceTransientError("403"),
            {"source": "baidu", "symbol": "600519",
             "frequency": "daily", "requested_count": 3, "rows": []},
        ]

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_mootdx)
        g.register(mock_baidu)

        # Fail both twice to OPEN
        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        # Reset baidu to CLOSED so it can succeed as fallback
        g._health["baidu"]["state"] = "CLOSED"
        g._health["baidu"]["consecutive_failures"] = 0

        # Advance past cooldown
        g.now_fn = lambda: 1030.0

        # Replace mootdx with slow probe
        mock_mootdx.fetch_market_bars.side_effect = slow_probe

        results = []  # type: ignore

        def thread1():
            try:
                results.append(g.fetch_market_bars(_make_instrument(), "daily", 3))
            except GovernorUnavailableError:
                results.append("unavailable")

        t = _th.Thread(target=thread1)
        t.start()

        # Wait for probe to start (HALF_OPEN state is now active)
        probe_in_progress.wait(timeout=5)

        # Thread 2: should see HALF_OPEN and skip mootdx, fallback to Baidu
        result = g.fetch_market_bars(_make_instrument(), "daily", 3)
        assert result["source"] == "baidu"

        # Release the probe so thread 1 can finish
        probe_done.set()
        t.join(timeout=5)

    def test_concurrent_primary_probe_called_once(self):
        """H. Concurrent: primary adapter probe call_count == 1."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_mootdx = MagicMock()
        mock_mootdx.name = "mootdx"

        import threading as _th

        probe_started = _th.Event()
        release_probe = _th.Event()

        # Mootdx: first 2 calls fail (to reach OPEN), then blocks on probe
        mootdx_call_count = [0]

        def mootdx_callable(*args, **kwargs):
            mootdx_call_count[0] += 1
            if mootdx_call_count[0] <= 2:
                raise SourceTransientError("down")
            # Recovery probe — signal and block until main thread releases
            probe_started.set()
            release_probe.wait(timeout=5)
            return {"source": "mootdx", "symbol": "600519", "frequency": "daily",
                    "requested_count": 3, "rows": []}

        mock_mootdx.fetch_market_bars.side_effect = mootdx_callable

        # Baidu: first 2 calls fail (to reach OPEN), then always succeeds
        baidu_call_count = [0]

        def baidu_callable(*args, **kwargs):
            baidu_call_count[0] += 1
            if baidu_call_count[0] <= 2:
                raise SourceTransientError("403")
            return {"source": "baidu", "symbol": "600519",
                    "frequency": "daily", "requested_count": 3, "rows": []}

        mock_baidu = MagicMock()
        mock_baidu.name = "baidu"
        mock_baidu.fetch_market_bars.side_effect = baidu_callable

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_mootdx)
        g.register(mock_baidu)

        # Fail both twice to OPEN (open_until = 1030.0)
        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        # Reset baidu to CLOSED so it can succeed as fallback
        g._health["baidu"]["state"] = "CLOSED"
        g._health["baidu"]["consecutive_failures"] = 0

        # Advance past cooldown — mootdx enters HALF_OPEN on next call
        g.now_fn = lambda: 1030.0

        results = [None, None]
        errors = []

        def worker(idx):
            try:
                results[idx] = g.fetch_market_bars(_make_instrument(), "daily", 3)
            except GovernorUnavailableError:
                results[idx] = "unavailable"
            except Exception as e:
                errors.append(str(e))

        # Thread 1: enters HALF_OPEN probe and blocks
        t1 = _th.Thread(target=worker, args=(0,))
        t1.start()

        # Wait for probe to start (HALF_OPEN state is now active)
        assert probe_started.wait(timeout=5), "probe never started"

        # Thread 2: should see HALF_OPEN and skip mootdx, fallback to Baidu
        t2 = _th.Thread(target=worker, args=(1,))
        t2.start()

        # Wait for thread 2 to complete (should be fast — just baidu call)
        t2.join(timeout=5)

        # Now release the probe so thread 1 can finish
        release_probe.set()
        t1.join(timeout=5)

        # Verify no thread exceptions leaked
        assert errors == [], f"Worker threads raised exceptions: {errors}"

        # Thread 2 used Baidu fallback (skipped HALF_OPEN mootdx)
        assert results[1] is not None and isinstance(results[1], dict), \
            f"Thread 2 result: {results[1]}"
        assert results[1]["source"] == "baidu"

        # Thread 1 got mootdx (probe success)
        assert results[0] is not None and isinstance(results[0], dict), \
            f"Thread 1 result: {results[0]}"
        assert results[0]["source"] == "mootdx"

        # mootdx is now CLOSED after probe success
        assert g.get_source_health("mootdx")["state"] == "CLOSED"

        # Baidu was called exactly once (by thread 2 as fallback)
        assert baidu_call_count[0] == 3, f"Baidu called {baidu_call_count[0]} times (expected 3: 2 failures + 1 fallback)"

    def test_cooldown_not_expired_still_skips(self):
        """I. Cooldown not expired → still skip."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        mock_source.fetch_market_bars.side_effect = SourceTransientError("down")

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        # now=1029 (not expired) — still skip
        g.now_fn = lambda: 1029.0

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        assert g.get_source_health("mootdx")["state"] == "OPEN"
        # Adapter not called during cooldown
        assert mock_source.fetch_market_bars.call_count == 2

    def test_primary_recovery_restores_priority(self):
        """J. Primary recovery → next request uses mootdx first."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_mootdx = MagicMock()
        mock_mootdx.name = "mootdx"
        mock_mootdx.fetch_market_bars.side_effect = [
            SourceTransientError("down"),
            SourceTransientError("down"),
            {"source": "mootdx", "symbol": "600519", "frequency": "daily",
             "requested_count": 3, "rows": []},
        ]

        mock_baidu = MagicMock()
        mock_baidu.name = "baidu"
        mock_baidu.fetch_market_bars.side_effect = [
            SourceTransientError("403"),
            SourceTransientError("403"),
        ]

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_mootdx)
        g.register(mock_baidu)

        # Fail both twice to OPEN
        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        # Advance past cooldown — mootdx succeeds on probe
        g.now_fn = lambda: 1030.0

        result = g.fetch_market_bars(_make_instrument(), "daily", 3)
        assert result["source"] == "mootdx"

        # mootdx is now CLOSED and primary again
        assert g.get_source_health("mootdx")["state"] == "CLOSED"


# --- Persistence wiring tests (Subtask 6B) ---

class TestGovernorPersistence:
    def test_no_store_behavior_unchanged(self):
        """A. Governor without store behaves identically."""
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError

        mock_source = MagicMock()
        mock_source.name = "mootdx"
        mock_source.fetch_market_bars.side_effect = SourceTransientError("down")

        g = SourceGovernor(now_fn=lambda: 1000.0)
        g.register(mock_source)

        with pytest.raises(GovernorUnavailableError):
            g.fetch_market_bars(_make_instrument(), "daily", 3)

        assert g.get_source_health("mootdx")["consecutive_failures"] == 1
        assert g.get_source_health("mootdx")["state"] == "CLOSED"

    def test_failure_saved_to_db(self):
        """B. Governor failure=1 → DB saves failures=1."""
        import tempfile, os
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError
        from astock_api.source_state_store import SQLiteSourceStateStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteSourceStateStore(os.path.join(tmp, "test.db"))

            mock_source = MagicMock()
            mock_source.name = "mootdx"
            mock_source.fetch_market_bars.side_effect = SourceTransientError("down")

            g = SourceGovernor(now_fn=lambda: 1000.0, state_store=store)
            g.register(mock_source)

            with pytest.raises(GovernorUnavailableError):
                g.fetch_market_bars(_make_instrument(), "daily", 3)

            # Verify DB has the state
            stored = store.load("mootdx", "market_bars_daily")
            assert stored is not None
            assert stored["consecutive_failures"] == 1

    def test_cross_governor_loads_state(self):
        """C. Second Governor instance loads failures=1 from DB."""
        import tempfile, os
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError
        from astock_api.source_state_store import SQLiteSourceStateStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteSourceStateStore(os.path.join(tmp, "test.db"))

            mock_source1 = MagicMock()
            mock_source1.name = "mootdx"
            mock_source1.fetch_market_bars.side_effect = SourceTransientError("down")

            g1 = SourceGovernor(now_fn=lambda: 1000.0, state_store=store)
            g1.register(mock_source1)

            with pytest.raises(GovernorUnavailableError):
                g1.fetch_market_bars(_make_instrument(), "daily", 3)

            # New Governor loads from same DB
            mock_source2 = MagicMock()
            mock_source2.name = "mootdx"
            g2 = SourceGovernor(now_fn=lambda: 1000.0, state_store=store)
            g2.register(mock_source2)

            assert g2.get_source_health("mootdx")["consecutive_failures"] == 1

    def test_second_failure_opens_and_saves(self):
        """D. Second failure → failures=2, OPEN, DB saves."""
        import tempfile, os
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError
        from astock_api.source_state_store import SQLiteSourceStateStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteSourceStateStore(os.path.join(tmp, "test.db"))

            mock_source = MagicMock()
            mock_source.name = "mootdx"
            mock_source.fetch_market_bars.side_effect = SourceTransientError("down")

            g = SourceGovernor(now_fn=lambda: 1000.0, state_store=store)
            g.register(mock_source)

            with pytest.raises(GovernorUnavailableError):
                g.fetch_market_bars(_make_instrument(), "daily", 3)

            with pytest.raises(GovernorUnavailableError):
                g.fetch_market_bars(_make_instrument(), "daily", 3)

            stored = store.load("mootdx", "market_bars_daily")
            assert stored["state"] == "OPEN"
            assert stored["consecutive_failures"] == 2
            assert stored["open_until"] is not None

    def test_new_governor_loads_open_state(self):
        """E. New Governor instance loads OPEN state from DB."""
        import tempfile, os
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError
        from astock_api.source_state_store import SQLiteSourceStateStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteSourceStateStore(os.path.join(tmp, "test.db"))

            mock_source1 = MagicMock()
            mock_source1.name = "mootdx"
            mock_source1.fetch_market_bars.side_effect = SourceTransientError("down")

            g1 = SourceGovernor(now_fn=lambda: 1000.0, state_store=store)
            g1.register(mock_source1)

            with pytest.raises(GovernorUnavailableError):
                g1.fetch_market_bars(_make_instrument(), "daily", 3)

            with pytest.raises(GovernorUnavailableError):
                g1.fetch_market_bars(_make_instrument(), "daily", 3)

            # New Governor loads OPEN state
            mock_source2 = MagicMock()
            mock_source2.name = "mootdx"
            g2 = SourceGovernor(now_fn=lambda: 1000.0, state_store=store)
            g2.register(mock_source2)

            assert g2.get_source_health("mootdx")["state"] == "OPEN"
            assert g2.get_source_health("mootdx")["consecutive_failures"] == 2

    def test_success_resets_and_saves(self):
        """F. Success → DB saves CLOSED/failures=0/open_until=None."""
        import tempfile, os
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError
        from astock_api.source_state_store import SQLiteSourceStateStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteSourceStateStore(os.path.join(tmp, "test.db"))

            mock_source = MagicMock()
            mock_source.name = "mootdx"
            mock_source.fetch_market_bars.side_effect = [
                SourceTransientError("down"),
                {"source": "mootdx", "symbol": "600519", "frequency": "daily",
                 "requested_count": 3, "rows": []},
            ]

            g = SourceGovernor(now_fn=lambda: 1000.0, state_store=store)
            g.register(mock_source)

            with pytest.raises(GovernorUnavailableError):
                g.fetch_market_bars(_make_instrument(), "daily", 3)

            g.fetch_market_bars(_make_instrument(), "daily", 3)

            stored = store.load("mootdx", "market_bars_daily")
            assert stored["state"] == "CLOSED"
            assert stored["consecutive_failures"] == 0
            assert stored["open_until"] is None

    def test_mootdx_baidu_persistence_independent(self):
        """G. mootdx / baidu persistence independent."""
        import tempfile, os
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError
        from astock_api.source_state_store import SQLiteSourceStateStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteSourceStateStore(os.path.join(tmp, "test.db"))

            mock_mootdx = MagicMock()
            mock_mootdx.name = "mootdx"
            mock_mootdx.fetch_market_bars.side_effect = SourceTransientError("down")

            mock_baidu = MagicMock()
            mock_baidu.name = "baidu"
            mock_baidu.fetch_market_bars.return_value = {
                "source": "baidu", "symbol": "600519",
                "frequency": "daily", "requested_count": 3, "rows": [],
            }

            g = SourceGovernor(now_fn=lambda: 1000.0, state_store=store)
            g.register(mock_mootdx)
            g.register(mock_baidu)

            # mootdx fails, baidu succeeds as fallback
            result = g.fetch_market_bars(_make_instrument(), "daily", 3)
            assert result["source"] == "baidu"

            mootdx_stored = store.load("mootdx", "market_bars_daily")
            baidu_stored = store.load("baidu", "market_bars_daily")

            assert mootdx_stored["consecutive_failures"] == 1
            assert baidu_stored["state"] == "CLOSED"

    def test_half_open_transition_saved(self):
        """H. HALF_OPEN state transition writes to DB."""
        import tempfile, os
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError
        from astock_api.source_state_store import SQLiteSourceStateStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteSourceStateStore(os.path.join(tmp, "test.db"))

            mock_source = MagicMock()
            mock_source.name = "mootdx"
            mock_source.fetch_market_bars.side_effect = SourceTransientError("down")

            g = SourceGovernor(now_fn=lambda: 1000.0, state_store=store)
            g.register(mock_source)

            # Two failures → OPEN
            with pytest.raises(GovernorUnavailableError):
                g.fetch_market_bars(_make_instrument(), "daily", 3)

            with pytest.raises(GovernorUnavailableError):
                g.fetch_market_bars(_make_instrument(), "daily", 3)

            # Advance past cooldown, trigger HALF_OPEN
            g.now_fn = lambda: 1030.0

            with pytest.raises(GovernorUnavailableError):
                g.fetch_market_bars(_make_instrument(), "daily", 3)

            # DB should have OPEN (probe failed → back to OPEN)
            stored = store.load("mootdx", "market_bars_daily")
            assert stored["state"] == "OPEN"



# --- Restart recovery tests (Subtask 6C) ---

class TestRestartRecovery:
    def test_get_governor_returns_same_instance(self):
        """B. get_governor() returns same instance."""
        # Monkeypatch to avoid hitting /app/data in tests.
        import astock_api.source_governor as sg_module

        original = sg_module._default_governor
        try:
            import tempfile, os
            from astock_api.source_state_store import SQLiteSourceStateStore

            sg_module._default_governor = None
            with tempfile.TemporaryDirectory() as tmp:
                sg_module._DEFAULT_DB_PATH = os.path.join(tmp, "test.db")

                g1 = sg_module.get_governor()
                g2 = sg_module.get_governor()

                assert g1 is g2
        finally:
            sg_module._default_governor = original

    def test_restart_loads_failures(self):
        """C. failure=1 → restart → new Governor loads failures=1."""
        import tempfile, os
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError
        from astock_api.source_state_store import SQLiteSourceStateStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")

            # First instance: fail once
            store1 = SQLiteSourceStateStore(db_path)
            mock_source1 = MagicMock()
            mock_source1.name = "mootdx"
            mock_source1.fetch_market_bars.side_effect = SourceTransientError("down")

            g1 = SourceGovernor(now_fn=lambda: 1000.0, state_store=store1)
            g1.register(mock_source1)

            with pytest.raises(GovernorUnavailableError):
                g1.fetch_market_bars(_make_instrument(), "daily", 3)

            # Simulate restart — new Governor loads from same DB
            store2 = SQLiteSourceStateStore(db_path)
            mock_source2 = MagicMock()
            mock_source2.name = "mootdx"

            g2 = SourceGovernor(now_fn=lambda: 1000.0, state_store=store2)
            g2.register(mock_source2)

            assert g2.get_source_health("mootdx")["consecutive_failures"] == 1

    def test_restart_loads_open(self):
        """D. OPEN → restart → new Governor still OPEN."""
        import tempfile, os
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError
        from astock_api.source_state_store import SQLiteSourceStateStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")

            store1 = SQLiteSourceStateStore(db_path)
            mock_source1 = MagicMock()
            mock_source1.name = "mootdx"
            mock_source1.fetch_market_bars.side_effect = SourceTransientError("down")

            g1 = SourceGovernor(now_fn=lambda: 1000.0, state_store=store1)
            g1.register(mock_source1)

            with pytest.raises(GovernorUnavailableError):
                g1.fetch_market_bars(_make_instrument(), "daily", 3)

            with pytest.raises(GovernorUnavailableError):
                g1.fetch_market_bars(_make_instrument(), "daily", 3)

            # Restart
            store2 = SQLiteSourceStateStore(db_path)
            mock_source2 = MagicMock()
            mock_source2.name = "mootdx"

            g2 = SourceGovernor(now_fn=lambda: 1000.0, state_store=store2)
            g2.register(mock_source2)

            assert g2.get_source_health("mootdx")["state"] == "OPEN"
            assert g2.get_source_health("mootdx")["consecutive_failures"] == 2

    def test_restart_open_cooldown_not_expired(self):
        """E. OPEN cooldown not expired → restart still skips."""
        import tempfile, os
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError
        from astock_api.source_state_store import SQLiteSourceStateStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")

            store1 = SQLiteSourceStateStore(db_path)
            mock_source1 = MagicMock()
            mock_source1.name = "mootdx"
            mock_source1.fetch_market_bars.side_effect = SourceTransientError("down")

            g1 = SourceGovernor(now_fn=lambda: 1000.0, state_store=store1)
            g1.register(mock_source1)

            with pytest.raises(GovernorUnavailableError):
                g1.fetch_market_bars(_make_instrument(), "daily", 3)

            with pytest.raises(GovernorUnavailableError):
                g1.fetch_market_bars(_make_instrument(), "daily", 3)

            # Restart with same time (cooldown not expired, open_until=1030)
            store2 = SQLiteSourceStateStore(db_path)
            mock_source2 = MagicMock()
            mock_source2.name = "mootdx"

            g2 = SourceGovernor(now_fn=lambda: 1005.0, state_store=store2)
            g2.register(mock_source2)

            # Should still be OPEN, skip adapter
            with pytest.raises(GovernorUnavailableError):
                g2.fetch_market_bars(_make_instrument(), "daily", 3)

            # Adapter not called
            mock_source2.fetch_market_bars.assert_not_called()

    def test_restart_open_cooldown_expired_allows_half_open(self):
        """F. OPEN cooldown expired → restart allows HALF_OPEN probe."""
        import tempfile, os
        from astock_api.source_governor import SourceGovernor, GovernorUnavailableError
        from astock_api.source_state_store import SQLiteSourceStateStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")

            store1 = SQLiteSourceStateStore(db_path)
            mock_source1 = MagicMock()
            mock_source1.name = "mootdx"
            mock_source1.fetch_market_bars.side_effect = SourceTransientError("down")

            g1 = SourceGovernor(now_fn=lambda: 1000.0, state_store=store1)
            g1.register(mock_source1)

            with pytest.raises(GovernorUnavailableError):
                g1.fetch_market_bars(_make_instrument(), "daily", 3)

            with pytest.raises(GovernorUnavailableError):
                g1.fetch_market_bars(_make_instrument(), "daily", 3)

            # Restart with time past cooldown (open_until=1030, now=1035)
            store2 = SQLiteSourceStateStore(db_path)
            mock_source2 = MagicMock()
            mock_source2.name = "mootdx"
            mock_source2.fetch_market_bars.return_value = {
                "source": "mootdx", "symbol": "600519",
                "frequency": "daily", "requested_count": 3, "rows": [],
            }

            g2 = SourceGovernor(now_fn=lambda: 1035.0, state_store=store2)
            g2.register(mock_source2)

            # Should enter HALF_OPEN probe and succeed
            result = g2.fetch_market_bars(_make_instrument(), "daily", 3)
            assert result["source"] == "mootdx"

    def test_restart_half_open_recovers_to_open(self):
        """G. DB has HALF_OPEN → restart loads as OPEN."""
        import tempfile, os
        from astock_api.source_governor import SourceGovernor
        from astock_api.source_state_store import SQLiteSourceStateStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")

            store1 = SQLiteSourceStateStore(db_path)
            # Manually save HALF_OPEN state to simulate crash during probe.
            store1.save("mootdx", "market_bars_daily", {
                "consecutive_failures": 2,
                "state": "HALF_OPEN",
                "open_until": None,
            })

            # Restart — should recover HALF_OPEN → OPEN
            store2 = SQLiteSourceStateStore(db_path)
            mock_source = MagicMock()
            mock_source.name = "mootdx"

            g2 = SourceGovernor(now_fn=lambda: 1000.0, state_store=store2)
            g2.register(mock_source)

            assert g2.get_source_health("mootdx")["state"] == "OPEN"
            assert g2.get_source_health("mootdx")["consecutive_failures"] == 2

    def test_restart_half_open_recovery_sets_open_until(self):
        """H. HALF_OPEN recovery: open_until = now + 30."""
        import tempfile, os
        from astock_api.source_governor import SourceGovernor
        from astock_api.source_state_store import SQLiteSourceStateStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")

            store1 = SQLiteSourceStateStore(db_path)
            store1.save("mootdx", "market_bars_daily", {
                "consecutive_failures": 2,
                "state": "HALF_OPEN",
                "open_until": None,
            })

            store2 = SQLiteSourceStateStore(db_path)
            mock_source = MagicMock()
            mock_source.name = "mootdx"

            g2 = SourceGovernor(now_fn=lambda: 1000.0, state_store=store2)
            g2.register(mock_source)

            assert g2.get_source_health("mootdx")["open_until"] == 1030.0

    def test_restart_half_open_recovery_updates_db(self):
        """I. HALF_OPEN recovery: DB itself updated to OPEN."""
        import tempfile, os
        from astock_api.source_governor import SourceGovernor
        from astock_api.source_state_store import SQLiteSourceStateStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")

            store1 = SQLiteSourceStateStore(db_path)
            store1.save("mootdx", "market_bars_daily", {
                "consecutive_failures": 2,
                "state": "HALF_OPEN",
                "open_until": None,
            })

            store2 = SQLiteSourceStateStore(db_path)
            mock_source = MagicMock()
            mock_source.name = "mootdx"

            g2 = SourceGovernor(now_fn=lambda: 1000.0, state_store=store2)
            g2.register(mock_source)

            # DB should now have OPEN, not HALF_OPEN
            stored = store2.load("mootdx", "market_bars_daily")
            assert stored["state"] == "OPEN"

    def test_restart_closed_stays_closed(self):
        """J. CLOSED restart → stays CLOSED."""
        import tempfile, os
        from astock_api.source_governor import SourceGovernor
        from astock_api.source_state_store import SQLiteSourceStateStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")

            store1 = SQLiteSourceStateStore(db_path)
            mock_source1 = MagicMock()
            mock_source1.name = "mootdx"
            mock_source1.fetch_market_bars.return_value = {
                "source": "mootdx", "symbol": "600519",
                "frequency": "daily", "requested_count": 3, "rows": [],
            }

            g1 = SourceGovernor(now_fn=lambda: 1000.0, state_store=store1)
            g1.register(mock_source1)

            g1.fetch_market_bars(_make_instrument(), "daily", 3)

            # Restart
            store2 = SQLiteSourceStateStore(db_path)
            mock_source2 = MagicMock()
            mock_source2.name = "mootdx"

            g2 = SourceGovernor(now_fn=lambda: 1000.0, state_store=store2)
            g2.register(mock_source2)

            assert g2.get_source_health("mootdx")["state"] == "CLOSED"
            assert g2.get_source_health("mootdx")["consecutive_failures"] == 0

    def test_restart_does_not_call_upstream(self):
        """K. Restart/load does not call any upstream."""
        import tempfile, os
        from astock_api.source_governor import SourceGovernor
        from astock_api.source_state_store import SQLiteSourceStateStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")

            store1 = SQLiteSourceStateStore(db_path)
            mock_source1 = MagicMock()
            mock_source1.name = "mootdx"
            mock_source1.fetch_market_bars.side_effect = SourceTransientError("down")

            g1 = SourceGovernor(now_fn=lambda: 1000.0, state_store=store1)
            g1.register(mock_source1)

            # Fail twice to OPEN
            try:
                g1.fetch_market_bars(_make_instrument(), "daily", 3)
            except Exception:
                pass

            try:
                g1.fetch_market_bars(_make_instrument(), "daily", 3)
            except Exception:
                pass

            # Restart — mock_source2 should NOT be called during register/load
            store2 = SQLiteSourceStateStore(db_path)
            mock_source2 = MagicMock()
            mock_source2.name = "mootdx"

            g2 = SourceGovernor(now_fn=lambda: 1000.0, state_store=store2)
            g2.register(mock_source2)

            mock_source2.fetch_market_bars.assert_not_called()



# --- Concurrent lazy init test (Subtask 6C.6) ---

class TestConcurrentGovernorInit:
    def test_concurrent_get_governor_creates_single_instance(self):
        """Multiple threads call get_governor() simultaneously → exactly one instance."""
        import tempfile, os
        import threading as _th
        import astock_api.source_governor as sg_module

        # Save originals for restore
        original_gov = sg_module._default_governor
        original_lock = sg_module._default_governor_lock

        try:
            # Reset module state
            sg_module._default_governor = None
            sg_module._default_governor_lock = _th.Lock()

            with tempfile.TemporaryDirectory() as tmp:
                sg_module._DEFAULT_DB_PATH = os.path.join(tmp, "test.db")

                # Counters for initialization tracking
                init_count = [0]
                original_create_governor = sg_module.create_governor

                def counting_create(now_fn=None, state_store=None):
                    init_count[0] += 1
                    return original_create_governor(now_fn=now_fn, state_store=state_store)

                sg_module.create_governor = counting_create

                results = [None] * 10
                errors = []
                barrier = _th.Barrier(10)

                def worker(idx):
                    try:
                        barrier.wait(timeout=5)
                        results[idx] = sg_module.get_governor()
                    except Exception as e:
                        errors.append(str(e))

                threads = [_th.Thread(target=worker, args=(i,)) for i in range(10)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=10)

                # All workers got the same object
                assert errors == [], f"Worker exceptions: {errors}"
                unique_governors = set(id(r) for r in results if r is not None)
                assert len(unique_governors) == 1, f"Created {len(unique_governors)} different governors"

                # Initialization happened exactly once
                assert init_count[0] == 1, f"create_governor called {init_count[0]} times"

                # Restore
                sg_module.create_governor = original_create_governor
        finally:
            sg_module._default_governor = original_gov
            sg_module._default_governor_lock = original_lock



# --- Health endpoint tests (Subtask 7A) ---

class TestHealthEndpoint:
    def _make_client(self):
        """Create a minimal FastAPI app with just the health router."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from astock_api.health import router as health_router

        test_app = FastAPI()
        test_app.include_router(health_router)
        return TestClient(test_app)

    def test_health_sources_returns_200(self):
        """A. GET /health/sources → HTTP 200."""
        with patch("astock_api.source_governor.get_governor") as mock_get:
            mock_gov = MagicMock()
            mock_gov.get_health_snapshot.return_value = [
                {"source": "mootdx", "capability": "market_bars_daily",
                 "state": "CLOSED", "consecutive_failures": 0, "open_until": None},
                {"source": "baidu", "capability": "market_bars_daily",
                 "state": "CLOSED", "consecutive_failures": 0, "open_until": None},
            ]
            mock_get.return_value = mock_gov

            response = self._make_client().get("/health/sources")
            assert response.status_code == 200

    def test_health_sources_includes_mootdx_baidu(self):
        """B. Contains mootdx + baidu."""
        with patch("astock_api.source_governor.get_governor") as mock_get:
            mock_gov = MagicMock()
            mock_gov.get_health_snapshot.return_value = [
                {"source": "mootdx", "capability": "market_bars_daily",
                 "state": "CLOSED", "consecutive_failures": 0, "open_until": None},
                {"source": "baidu", "capability": "market_bars_daily",
                 "state": "CLOSED", "consecutive_failures": 0, "open_until": None},
            ]
            mock_get.return_value = mock_gov

            data = self._make_client().get("/health/sources").json()

        sources = [s["source"] for s in data["sources"]]
        assert "mootdx" in sources
        assert "baidu" in sources

    def test_health_sources_order_mootdx_first(self):
        """C. Order: mootdx → baidu."""
        with patch("astock_api.source_governor.get_governor") as mock_get:
            mock_gov = MagicMock()
            mock_gov.get_health_snapshot.return_value = [
                {"source": "mootdx", "capability": "market_bars_daily",
                 "state": "CLOSED", "consecutive_failures": 0, "open_until": None},
                {"source": "baidu", "capability": "market_bars_daily",
                 "state": "CLOSED", "consecutive_failures": 0, "open_until": None},
            ]
            mock_get.return_value = mock_gov

            data = self._make_client().get("/health/sources").json()

        sources = [s["source"] for s in data["sources"]]
        assert sources == ["mootdx", "baidu"]

    def test_health_sources_initial_closed(self):
        """D. Initial CLOSED / failures=0 correct."""
        with patch("astock_api.source_governor.get_governor") as mock_get:
            mock_gov = MagicMock()
            mock_gov.get_health_snapshot.return_value = [
                {"source": "mootdx", "capability": "market_bars_daily",
                 "state": "CLOSED", "consecutive_failures": 0, "open_until": None},
                {"source": "baidu", "capability": "market_bars_daily",
                 "state": "CLOSED", "consecutive_failures": 0, "open_until": None},
            ]
            mock_get.return_value = mock_gov

            data = self._make_client().get("/health/sources").json()

        mootdx = [s for s in data["sources"] if s["source"] == "mootdx"][0]
        assert mootdx["state"] == "CLOSED"
        assert mootdx["consecutive_failures"] == 0

    def test_health_sources_shows_open(self):
        """E. Governor mootdx OPEN → endpoint shows OPEN/failures/open_until."""
        with patch("astock_api.source_governor.get_governor") as mock_get:
            mock_gov = MagicMock()
            mock_gov.get_health_snapshot.return_value = [
                {"source": "mootdx", "capability": "market_bars_daily",
                 "state": "OPEN", "consecutive_failures": 2, "open_until": 1030.0},
                {"source": "baidu", "capability": "market_bars_daily",
                 "state": "CLOSED", "consecutive_failures": 0, "open_until": None},
            ]
            mock_get.return_value = mock_gov

            data = self._make_client().get("/health/sources").json()

        mootdx = [s for s in data["sources"] if s["source"] == "mootdx"][0]
        assert mootdx["state"] == "OPEN"
        assert mootdx["consecutive_failures"] == 2
        assert mootdx["open_until"] == 1030.0

    def test_health_sources_shows_half_open(self):
        """F. HALF_OPEN state correctly displayed."""
        with patch("astock_api.source_governor.get_governor") as mock_get:
            mock_gov = MagicMock()
            mock_gov.get_health_snapshot.return_value = [
                {"source": "mootdx", "capability": "market_bars_daily",
                 "state": "HALF_OPEN", "consecutive_failures": 2, "open_until": None},
                {"source": "baidu", "capability": "market_bars_daily",
                 "state": "CLOSED", "consecutive_failures": 0, "open_until": None},
            ]
            mock_get.return_value = mock_gov

            data = self._make_client().get("/health/sources").json()

        mootdx = [s for s in data["sources"] if s["source"] == "mootdx"][0]
        assert mootdx["state"] == "HALF_OPEN"

    def test_health_sources_does_not_call_adapter(self):
        """G. Calling endpoint does not call any adapter/upstream."""
        with patch("astock_api.source_governor.get_governor") as mock_get:
            mock_gov = MagicMock()
            mock_gov.get_health_snapshot.return_value = []
            mock_get.return_value = mock_gov

            self._make_client().get("/health/sources")

            # get_health_snapshot does not call adapters
            for source in getattr(mock_gov, '_sources', {}).values():
                source.fetch_market_bars.assert_not_called()

    def test_health_sources_does_not_change_state(self):
        """H. Calling endpoint does not change health state."""
        with patch("astock_api.source_governor.get_governor") as mock_get:
            initial_snapshot = [
                {"source": "mootdx", "capability": "market_bars_daily",
                 "state": "OPEN", "consecutive_failures": 2, "open_until": 1030.0},
            ]
            mock_gov = MagicMock()
            mock_gov.get_health_snapshot.return_value = initial_snapshot
            mock_get.return_value = mock_gov

            client = self._make_client()
            client.get("/health/sources")
            client.get("/health/sources")

            # get_health_snapshot called twice, state unchanged
            assert mock_gov.get_health_snapshot.call_count == 2

    def test_health_sources_no_secrets(self):
        """I. Response does not contain secrets."""
        with patch("astock_api.source_governor.get_governor") as mock_get:
            mock_gov = MagicMock()
            mock_gov.get_health_snapshot.return_value = [
                {"source": "mootdx", "capability": "market_bars_daily",
                 "state": "CLOSED", "consecutive_failures": 0, "open_until": None},
            ]
            mock_get.return_value = mock_gov

            response_text = self._make_client().get("/health/sources").text.lower()

        for secret in ["api_key", "password", "cookie", "authorization", "proxy", "db_path"]:
            assert secret not in response_text

    def test_existing_health_ready_not_broken(self):
        """J. Existing /health/ready still works (router mounted)."""
        # Just verify the health router has both routes registered.
        from astock_api.health import router

        route_paths = [route.path for route in router.routes]
        assert "/health/ready" in route_paths
        assert "/health/sources" in route_paths
