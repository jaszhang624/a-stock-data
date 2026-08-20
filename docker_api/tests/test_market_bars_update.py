"""R6-2: Incremental update engine targeted tests.

Tests cover idempotency, bootstrap behavior, identity isolation,
and NOOP semantics for market_bars_update handler.
"""

import json
import pytest
from unittest.mock import patch, MagicMock


def make_bars(dates, price=10.0):
    """Create bar dicts for given date strings."""
    return [{
        "datetime": d,
        "open": price,
        "high": price + 0.5,
        "low": price - 0.3,
        "close": price + 0.1,
        "volume": 1000,
        "amount": price * 100000,
    } for d in dates]


# ── A. Bootstrap: no existing data -> rows persisted ───────────────

def test_bootstrap_no_existing_data(tmp_path):
    """When no stored data exists, handler fetches bootstrap_count bars and persists them."""
    from astock_api.job_handlers import market_bars_update_handler

    duckdb_path = str(tmp_path / "test.duckdb")
    bars = make_bars(["2026-08-14", "2026-08-15", "2026-08-18", "2026-08-19", "2026-08-20"])

    mock_gov = MagicMock()
    mock_gov.fetch_market_bars.return_value = {
        "source": "mootdx",
        "symbol": "600519",
        "canonical_id": "SSE:600519",
        "exchange": "SSE",
        "frequency": "daily",
        "requested_count": 100,
        "rows": bars,
    }

    payload = {
        "symbol": "600519",
        "exchange": "SSE",
        "asset_type": "EQUITY",
        "frequency": "daily",
        "count": 10,
        "bootstrap_count": 100,
    }

    with patch("astock_api.job_handlers.get_governor", return_value=mock_gov):
        with patch("astock_api.dataset_store.DatasetStore") as MockStore:
            store = MagicMock()
            store.get_latest_trade_date.return_value = None
            MockStore.return_value = store

            result = market_bars_update_handler(payload)

    assert result["status"] == "UPDATED"
    assert result["latest_before"] is None
    store.write_market_bars.assert_called_once()


# ── B. Incremental: existing data D1, provider returns D1,D2,D3 -> only D2,D3 ──

def test_incremental_filters_existing(tmp_path):
    """When data exists through D1 and provider returns D1,D2,D3, only D2,D3 are written."""
    from astock_api.job_handlers import market_bars_update_handler

    bars = make_bars(["2026-08-15", "2026-08-18", "2026-08-19"])
    mock_gov = MagicMock()
    mock_gov.fetch_market_bars.return_value = {
        "source": "mootdx",
        "symbol": "600519",
        "canonical_id": "SSE:600519",
        "exchange": "SSE",
        "frequency": "daily",
        "requested_count": 10,
        "rows": bars,
    }

    payload = {
        "symbol": "600519",
        "exchange": "SSE",
        "asset_type": "EQUITY",
        "frequency": "daily",
        "count": 10,
    }

    with patch("astock_api.job_handlers.get_governor", return_value=mock_gov):
        with patch("astock_api.dataset_store.DatasetStore") as MockStore:
            store = MagicMock()
            store.get_latest_trade_date.return_value = "2026-08-15"
            MockStore.return_value = store

            result = market_bars_update_handler(payload)

    assert result["status"] == "UPDATED"
    assert result["latest_before"] == "2026-08-15"
    assert result["rows_inserted"] == 2  # D2, D3 only (D1 filtered)
    store.write_market_bars.assert_called_once()


# ── C. Idempotent rerun: same target -> NOOP, zero duplicates ───────

def test_idempotent_rerun_noop(tmp_path):
    """Rerunning with same target date results in NOOP - zero new rows."""
    from astock_api.job_handlers import market_bars_update_handler

    bars = make_bars(["2026-08-15", "2026-08-18"])
    mock_gov = MagicMock()
    mock_gov.fetch_market_bars.return_value = {
        "source": "mootdx",
        "symbol": "600519",
        "canonical_id": "SSE:600519",
        "exchange": "SSE",
        "frequency": "daily",
        "requested_count": 10,
        "rows": bars,
    }

    payload = {
        "symbol": "600519",
        "exchange": "SSE",
        "asset_type": "EQUITY",
        "frequency": "daily",
        "count": 10,
    }

    with patch("astock_api.job_handlers.get_governor", return_value=mock_gov):
        with patch("astock_api.dataset_store.DatasetStore") as MockStore:
            store = MagicMock()
            store.get_latest_trade_date.return_value = "2026-08-18"  # all bars already stored
            MockStore.return_value = store

            result = market_bars_update_handler(payload)

    assert result["status"] == "NOOP"
    assert result["rows_inserted"] == 0
    store.write_market_bars.assert_not_called()


# ── D. SSE:600519 equity identity correct ──────────────────────────

def test_sse_equity_identity(tmp_path):
    """SSE:600519 EQUITY identity is preserved through the handler."""
    from astock_api.job_handlers import market_bars_update_handler

    bars = make_bars(["2026-08-20"])
    mock_gov = MagicMock()
    mock_gov.fetch_market_bars.return_value = {
        "source": "mootdx",
        "symbol": "600519",
        "canonical_id": "SSE:600519",
        "exchange": "SSE",
        "frequency": "daily",
        "requested_count": 10,
        "rows": bars,
    }

    payload = {
        "symbol": "600519",
        "exchange": "SSE",
        "asset_type": "EQUITY",
        "frequency": "daily",
        "count": 10,
    }

    with patch("astock_api.job_handlers.get_governor", return_value=mock_gov):
        with patch("astock_api.dataset_store.DatasetStore") as MockStore:
            store = MagicMock()
            store.get_latest_trade_date.return_value = None
            MockStore.return_value = store

            result = market_bars_update_handler(payload)

    assert result["canonical_id"] == "SSE:600519"
    call_args = mock_gov.fetch_market_bars.call_args
    instrument = call_args[0][0]
    assert instrument.exchange == "SSE"
    assert instrument.code == "600519"
    assert instrument.asset_type == "EQUITY"


# ── E. SSE:000001 index identity correct ───────────────────────────

def test_sse_index_identity(tmp_path):
    """SSE:000001 INDEX identity is preserved through the handler."""
    from astock_api.job_handlers import market_bars_update_handler

    bars = make_bars(["2026-08-20"])
    mock_gov = MagicMock()
    mock_gov.fetch_market_bars.return_value = {
        "source": "mootdx",
        "symbol": "000001",
        "canonical_id": "SSE:000001",
        "exchange": "SSE",
        "frequency": "daily",
        "requested_count": 10,
        "rows": bars,
    }

    payload = {
        "symbol": "000001",
        "exchange": "SSE",
        "asset_type": "INDEX",
        "frequency": "daily",
        "count": 10,
    }

    with patch("astock_api.job_handlers.get_governor", return_value=mock_gov):
        with patch("astock_api.dataset_store.DatasetStore") as MockStore:
            store = MagicMock()
            store.get_latest_trade_date.return_value = None
            MockStore.return_value = store

            result = market_bars_update_handler(payload)

    assert result["canonical_id"] == "SSE:000001"
    call_args = mock_gov.fetch_market_bars.call_args
    instrument = call_args[0][0]
    assert instrument.exchange == "SSE"
    assert instrument.code == "000001"
    assert instrument.asset_type == "INDEX"


# ── F. SZSE:000001 equity state isolated from SSE:000001 index ─────

def test_cross_market_isolation(tmp_path):
    """SZSE:000001 EQUITY state is isolated from SSE:000001 INDEX."""
    from astock_api.job_handlers import market_bars_update_handler

    bars = make_bars(["2026-08-20"])
    mock_gov = MagicMock()
    mock_gov.fetch_market_bars.return_value = {
        "source": "mootdx",
        "symbol": "000001",
        "canonical_id": "SZSE:000001",
        "exchange": "SZSE",
        "frequency": "daily",
        "requested_count": 10,
        "rows": bars,
    }

    payload = {
        "symbol": "000001",
        "exchange": "SZSE",
        "asset_type": "EQUITY",
        "frequency": "daily",
        "count": 10,
    }

    with patch("astock_api.job_handlers.get_governor", return_value=mock_gov):
        with patch("astock_api.dataset_store.DatasetStore") as MockStore:
            store = MagicMock()
            store.get_latest_trade_date.return_value = None  # SZSE:000001 has no data
            MockStore.return_value = store

            result = market_bars_update_handler(payload)

    assert result["canonical_id"] == "SZSE:000001"
    store.get_latest_trade_date.assert_called_with("SZSE:000001")
    store.write_market_bars.assert_called_once()
    call_args = store.write_market_bars.call_args
    assert call_args[0][0] == "SZSE:000001"


# ── G. Provider returns empty -> NOOP, no corruption ───────────────

def test_provider_empty_noop(tmp_path):
    """When provider returns empty rows, handler reports NOOP without corrupting state."""
    from astock_api.job_handlers import market_bars_update_handler

    mock_gov = MagicMock()
    mock_gov.fetch_market_bars.return_value = {
        "source": "mootdx",
        "symbol": "600519",
        "canonical_id": "SSE:600519",
        "exchange": "SSE",
        "frequency": "daily",
        "requested_count": 10,
        "rows": [],  # empty
    }

    payload = {
        "symbol": "600519",
        "exchange": "SSE",
        "asset_type": "EQUITY",
        "frequency": "daily",
        "count": 10,
    }

    with patch("astock_api.job_handlers.get_governor", return_value=mock_gov):
        with patch("astock_api.dataset_store.DatasetStore") as MockStore:
            store = MagicMock()
            store.get_latest_trade_date.return_value = "2026-08-15"
            MockStore.return_value = store

            result = market_bars_update_handler(payload)

    assert result["status"] == "NOOP"
    assert result["rows_inserted"] == 0
    store.write_market_bars.assert_not_called()


# ── H. JobEngine registration does not change existing semantics ───

def test_job_engine_market_bars_update_registered():
    """market_bars_update is registered in JOB_TYPES and get_handler."""
    from astock_api.job_engine import JOB_TYPES
    from astock_api.job_handlers import get_handler

    assert "market_bars_update" in JOB_TYPES
    handler = get_handler("market_bars_update")
    assert handler is not None


def test_job_engine_create_market_bars_update(tmp_path):
    """JobEngine.create_job accepts market_bars_update and creates chunks."""
    from astock_api.job_engine import JobEngine

    db_path = str(tmp_path / "job_engine.duckdb")
    engine = JobEngine(db_path, str(tmp_path))
    engine.initialize()
    result = engine.create_job("market_bars_update", {
        "instruments": [
            {"code": "600519", "exchange": "SSE", "asset_type": "EQUITY"},
            {"code": "000001", "exchange": "SSE", "asset_type": "INDEX"},
        ],
        "frequency": "daily",
        "count": 10,
        "target_date": "2026-08-20",
    })

    assert result["status"] == "PENDING"
    job = engine.get_job(result["job_id"])
    assert job is not None
    assert job["total_chunks"] == 2

    chunks = engine.get_chunks(result["job_id"])
    assert len(chunks) == 2

    # Verify chunk payloads contain update-specific fields
    for c in chunks:
        payload = json.loads(c["payload_json"])
        assert "target_date" in payload
        assert "bootstrap_count" in payload


# ── DatasetStore.get_latest_trade_date tests (real DuckDB) ─────────

def test_get_latest_trade_date_no_data():
    """get_latest_trade_date returns None when no data exists."""
    from astock_api.dataset_store import DatasetStore

    store = DatasetStore(":memory:")
    store.bootstrap()

    result = store.get_latest_trade_date("SSE:600519")
    assert result is None


def test_get_latest_trade_date_with_data():
    """get_latest_trade_date returns correct latest date."""
    from astock_api.dataset_store import DatasetStore

    store = DatasetStore(":memory:")
    store.bootstrap()

    # Insert bars
    store.write_market_bars("SSE:600519", [
        {"trade_date": "2026-08-15", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100, "amount": 1000},
        {"trade_date": "2026-08-18", "open": 10.5, "high": 12, "low": 10, "close": 11, "volume": 200, "amount": 2000},
        {"trade_date": "2026-08-19", "open": 11, "high": 12.5, "low": 10.8, "close": 12, "volume": 300, "amount": 3000},
    ], "mootdx", "test-job")

    result = store.get_latest_trade_date("SSE:600519")
    assert result == "2026-08-19"


def test_get_latest_trade_date_identity_isolation():
    """SSE:000001 INDEX query does not return SZSE:000001 EQUITY data."""
    from astock_api.dataset_store import DatasetStore

    store = DatasetStore(":memory:")
    store.bootstrap()

    # Insert data for SZSE:000001 EQUITY
    store.write_market_bars("SZSE:000001", [
        {"trade_date": "2026-08-20", "open": 15, "high": 16, "low": 14, "close": 15.5, "volume": 500, "amount": 7500},
    ], "mootdx", "test-job")

    # Query SSE:000001 INDEX - should return None (different security_id)
    sse_result = store.get_latest_trade_date("SSE:000001")
    assert sse_result is None

    # Query SZSE:000001 EQUITY - should return the date
    szse_result = store.get_latest_trade_date("SZSE:000001")
    assert szse_result == "2026-08-20"
