"""R6-3: Coverage / Freshness State targeted tests.

Tests cover freshness semantics, cross-market isolation, coverage aggregation,
and index seed labeling.
"""

import json
import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def in_memory_store():
    """Create an in-memory DatasetStore with bootstrapped schema."""
    from astock_api.dataset_store import DatasetStore
    store = DatasetStore(":memory:")
    store.bootstrap()
    return store


class TestFreshnessModel:
    """Test freshness.py pure logic."""

    def test_missing_when_no_data(self):
        """A: instrument with no rows → MISSING."""
        from astock_api.freshness import assess_freshness
        assert assess_freshness(None, "2026-08-20") == "MISSING"

    def test_current_when_equal(self):
        """B: latest == reference_date → CURRENT."""
        from astock_api.freshness import assess_freshness
        assert assess_freshness("2026-08-20", "2026-08-20") == "CURRENT"

    def test_current_when_after(self):
        """C: latest > reference_date → CURRENT."""
        from astock_api.freshness import assess_freshness
        assert assess_freshness("2026-08-25", "2026-08-20") == "CURRENT"

    def test_stale_when_before(self):
        """D: latest < reference_date → STALE."""
        from astock_api.freshness import assess_freshness
        assert assess_freshness("2026-08-15", "2026-08-20") == "STALE"


class TestDatasetStoreStateQuery:
    """Test DatasetStore state query methods."""

    def test_get_instrument_state_no_data(self, in_memory_store):
        """Instrument with no rows returns None."""
        result = in_memory_store.get_instrument_state("SSE:600519")
        assert result is None

    def test_get_instrument_state_with_data(self, in_memory_store):
        """Instrument with data returns correct aggregates."""
        conn = in_memory_store.get_conn()
        conn.execute(
            "INSERT INTO market_bars_daily (security_id, trade_date, open, high, low, close, volume, source, ingested_at) VALUES (?, ?, 10, 11, 9, 10.5, 1000, 'test', '2026-08-19T00:00:00Z')",
            ("SSE:600519", "2026-08-18")
        )
        conn.execute(
            "INSERT INTO market_bars_daily (security_id, trade_date, open, high, low, close, volume, source, ingested_at) VALUES (?, ?, 10.5, 12, 10, 11, 1500, 'test', '2026-08-19T00:00:00Z')",
            ("SSE:600519", "2026-08-19")
        )
        conn.commit()

        result = in_memory_store.get_instrument_state("SSE:600519")
        assert result is not None
        assert result["security_id"] == "SSE:600519"
        assert result["row_count"] == 2
        assert result["earliest_trade_date"] == "2026-08-18"
        assert result["latest_trade_date"] == "2026-08-19"

    def test_get_all_instrument_states(self, in_memory_store):
        """Batch query returns all instruments without N+1."""
        conn = in_memory_store.get_conn()
        conn.execute(
            "INSERT INTO market_bars_daily (security_id, trade_date, open, high, low, close, volume, source, ingested_at) VALUES (?, ?, 10, 11, 9, 10.5, 1000, 'test', '2026-08-19T00:00:00Z')",
            ("SSE:600519", "2026-08-19")
        )
        conn.execute(
            "INSERT INTO market_bars_daily (security_id, trade_date, open, high, low, close, volume, source, ingested_at) VALUES (?, ?, 10, 11, 9, 10.5, 1000, 'test', '2026-08-19T00:00:00Z')",
            ("SZSE:000001", "2026-08-19")
        )
        conn.commit()

        results = in_memory_store.get_all_instrument_states()
        assert len(results) == 2
        ids = [r["security_id"] for r in results]
        assert "SSE:600519" in ids
        assert "SZSE:000001" in ids

    def test_cross_market_isolation(self, in_memory_store):
        """E: SSE:000001 INDEX and SZSE:000001 EQUITY maintain independent state."""
        conn = in_memory_store.get_conn()
        # SSE:000001 INDEX data
        conn.execute(
            "INSERT INTO market_bars_daily (security_id, trade_date, open, high, low, close, volume, source, ingested_at) VALUES (?, ?, 3000, 3100, 2900, 3050, 50000, 'test', '2026-08-19T00:00:00Z')",
            ("SSE:000001", "2026-08-19")
        )
        # SZSE:000001 EQUITY data (different dates)
        conn.execute(
            "INSERT INTO market_bars_daily (security_id, trade_date, open, high, low, close, volume, source, ingested_at) VALUES (?, ?, 10, 11, 9, 10.5, 1000, 'test', '2026-08-19T00:00:00Z')",
            ("SZSE:000001", "2026-08-15")
        )
        conn.commit()

        sse_state = in_memory_store.get_instrument_state("SSE:000001")
        szse_state = in_memory_store.get_instrument_state("SZSE:000001")

        assert sse_state is not None
        assert szse_state is not None
        assert sse_state["latest_trade_date"] == "2026-08-19"
        assert szse_state["latest_trade_date"] == "2026-08-15"
        assert sse_state["row_count"] == 1
        assert szse_state["row_count"] == 1


class TestCoverageSnapshot:
    """Test coverage snapshot generation."""

    def test_coverage_aggregation_counts(self, in_memory_store):
        """F: coverage aggregation counts correctly."""
        conn = in_memory_store.get_conn()
        # Insert data for some instruments
        for code, date in [("SSE:600519", "2026-08-19"), ("SZSE:000001", "2026-08-15")]:
            conn.execute(
                "INSERT INTO market_bars_daily (security_id, trade_date, open, high, low, close, volume, source, ingested_at) VALUES (?, ?, 10, 11, 9, 10.5, 1000, 'test', '2026-08-19T00:00:00Z')",
                (code, date)
            )
        conn.commit()

        # Create mock universe with 3 instruments
        tmpdir = tempfile.mkdtemp()
        universe_path = os.path.join(tmpdir, "universe.json")
        instruments = [
            {"canonical_id": "SSE:600519", "exchange": "SSE", "asset_type": "EQUITY"},
            {"canonical_id": "SZSE:000001", "exchange": "SZSE", "asset_type": "EQUITY"},
            {"canonical_id": "SSE:000001", "exchange": "SSE", "asset_type": "INDEX"},
        ]
        with open(universe_path, "w") as f:
            json.dump({"instruments": instruments}, f)

        from astock_api.coverage import generate_coverage_snapshot
        snapshot = generate_coverage_snapshot(
            in_memory_store, universe_path, "2026-08-19", tmpdir
        )

        assert snapshot["summary"]["total_universe"] == 3
        assert snapshot["summary"]["with_data"] == 2
        assert snapshot["summary"]["missing"] == 1

    def test_missing_instruments_appear_in_report(self, in_memory_store):
        """G: Universe instruments with no data appear as MISSING rather than disappearing."""
        tmpdir = tempfile.mkdtemp()
        universe_path = os.path.join(tmpdir, "universe.json")
        instruments = [
            {"canonical_id": "SSE:600519", "exchange": "SSE", "asset_type": "EQUITY"},
            {"canonical_id": "SZSE:000001", "exchange": "SZSE", "asset_type": "EQUITY"},
        ]
        with open(universe_path, "w") as f:
            json.dump({"instruments": instruments}, f)

        from astock_api.coverage import generate_coverage_snapshot
        snapshot = generate_coverage_snapshot(
            in_memory_store, universe_path, "2026-08-19", tmpdir
        )

        # Both instruments should appear, both MISSING
        assert len(snapshot["instruments"]) == 2
        for inst in snapshot["instruments"]:
            assert inst["freshness_status"] == "MISSING"

    def test_index_seed_labeled_correctly(self, in_memory_store):
        """H: index seed entries are labeled as supported seed, not full coverage."""
        tmpdir = tempfile.mkdtemp()
        universe_path = os.path.join(tmpdir, "universe.json")
        instruments = [
            {"canonical_id": "SSE:000001", "exchange": "SSE", "asset_type": "INDEX"},
            {"canonical_id": "SZSE:399001", "exchange": "SZSE", "asset_type": "INDEX"},
        ]
        with open(universe_path, "w") as f:
            json.dump({"instruments": instruments}, f)

        from astock_api.coverage import generate_coverage_snapshot
        snapshot = generate_coverage_snapshot(
            in_memory_store, universe_path, "2026-08-19", tmpdir
        )

        # Index breakdown should note it's seed-only
        assert snapshot["breakdown"]["index_seed"]["note"] == "supported_seed_indices_only"


class TestCoverageArtifact:
    """Test coverage artifact generation."""

    def test_artifact_written_to_coverage_dir(self, in_memory_store):
        """Coverage artifact is written to /app/data/coverage equivalent."""
        tmpdir = tempfile.mkdtemp()
        universe_path = os.path.join(tmpdir, "universe.json")
        instruments = [
            {"canonical_id": "SSE:600519", "exchange": "SSE", "asset_type": "EQUITY"},
        ]
        with open(universe_path, "w") as f:
            json.dump({"instruments": instruments}, f)

        from astock_api.coverage import generate_coverage_snapshot
        snapshot = generate_coverage_snapshot(
            in_memory_store, universe_path, "2026-08-19", tmpdir
        )

        artifact_path = os.path.join(tmpdir, "market_bars_daily_coverage.json")
        assert os.path.exists(artifact_path)

        with open(artifact_path, "r") as f:
            artifact = json.load(f)

        assert "generated_at" in artifact
        assert "reference_date" in artifact
        assert artifact["reference_date"] == "2026-08-19"
        assert "summary" in artifact
        assert "instruments" in artifact


class TestFreshnessIntegration:
    """Integration tests for freshness with DatasetStore."""

    def test_missing_via_store(self, in_memory_store):
        """Instrument not in DB → MISSING."""
        from astock_api.freshness import assess_freshness

        state = in_memory_store.get_instrument_state("SSE:600519")
        assert state is None
        assert assess_freshness(None, "2026-08-19") == "MISSING"

    def test_current_via_store(self, in_memory_store):
        """Instrument with recent data → CURRENT."""
        from astock_api.freshness import assess_freshness

        conn = in_memory_store.get_conn()
        conn.execute(
            "INSERT INTO market_bars_daily (security_id, trade_date, open, high, low, close, volume, source, ingested_at) VALUES (?, ?, 10, 11, 9, 10.5, 1000, 'test', '2026-08-19T00:00:00Z')",
            ("SSE:600519", "2026-08-19")
        )
        conn.commit()

        state = in_memory_store.get_instrument_state("SSE:600519")
        assert state is not None
        assert assess_freshness(state["latest_trade_date"], "2026-08-19") == "CURRENT"

    def test_stale_via_store(self, in_memory_store):
        """Instrument with old data → STALE."""
        from astock_api.freshness import assess_freshness

        conn = in_memory_store.get_conn()
        conn.execute(
            "INSERT INTO market_bars_daily (security_id, trade_date, open, high, low, close, volume, source, ingested_at) VALUES (?, ?, 10, 11, 9, 10.5, 1000, 'test', '2026-08-19T00:00:00Z')",
            ("SSE:600519", "2026-08-15")
        )
        conn.commit()

        state = in_memory_store.get_instrument_state("SSE:600519")
        assert state is not None
        assert assess_freshness(state["latest_trade_date"], "2026-08-19") == "STALE"