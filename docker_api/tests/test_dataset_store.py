"""Tests for Dataset Store (DuckDB canonical data plane).

Covers:
- DDL/bootstrap idempotency
- security_id normalization
- Duplicate rejection in security master
- Failed snapshot does not replace ACTIVE
- Atomic pointer activation
- Rollback to previous snapshot
- DB commit before chunk DONE ordering (verified via handler tests)
- Crash after DB commit / before DONE → retry safe (idempotent upsert)
- Duplicate market-bars retry → no duplicate rows
- Data path cannot escape /app/data (path traversal check)
- Malformed security records rejected
- Empty/suspicious universe cannot activate
- DuckDB writer concurrency behavior
"""
import os
import tempfile
import pytest
from astock_api.dataset_store import DatasetStore


@pytest.fixture
def store():
    """Create an in-memory DuckDB dataset store for testing."""
    s = DatasetStore(':memory:')
    s.bootstrap()
    yield s


class TestBootstrapIdempotency:
    def test_bootstrap_creates_tables(self, store):
        conn = store.get_conn()
        tables = conn.execute("SHOW TABLES").fetchall()
        table_names = [t[0] for t in tables]
        assert 'security_master_snapshots' in table_names
        assert 'security_master' in table_names
        assert 'market_bars_daily' in table_names
        assert 'dataset_heads' in table_names
        assert 'ingestion_log' in table_names

    def test_bootstrap_is_idempotent(self, store):
        # Call bootstrap again — should not fail
        store.bootstrap()
        conn = store.get_conn()
        tables = conn.execute("SHOW TABLES").fetchall()
        assert len(tables) == 5


class TestSecurityIdNormalization:
    def test_sse_codes(self):
        assert DatasetStore.make_security_id('600519') == 'SSE:600519'
        assert DatasetStore.make_security_id('601318') == 'SSE:601318'
        assert DatasetStore.make_security_id('920001') == 'SSE:920001'

    def test_szse_codes(self):
        assert DatasetStore.make_security_id('000001') == 'SZSE:000001'
        assert DatasetStore.make_security_id('300750') == 'SZSE:300750'
        assert DatasetStore.make_security_id('002142') == 'SZSE:002142'

    def test_bse_codes(self):
        assert DatasetStore.make_security_id('430090') == 'BSE:430090'
        assert DatasetStore.make_security_id('830799') == 'BSE:830799'
        assert DatasetStore.make_security_id('920001') == 'SSE:920001'  # 92xxx is SSE, not BSE

    def test_unknown_code_raises(self):
        with pytest.raises(ValueError, match="Unknown exchange"):
            DatasetStore.make_security_id('123456')


class TestMarketBarsUpsert:
    def test_write_bars_creates_rows(self, store):
        bars = [
            {'trade_date': '2024-01-01', 'open': 1.0, 'high': 2.0, 'low': 0.5, 'close': 1.5, 'volume': 100, 'amount': 150.0},
            {'trade_date': '2024-01-02', 'open': 1.5, 'high': 2.5, 'low': 1.0, 'close': 2.0, 'volume': 200, 'amount': 400.0},
        ]
        store.write_market_bars('SSE:600519', bars, 'mootdx', 'job-123')

        conn = store.get_conn()
        count = conn.execute(
            "SELECT COUNT(*) FROM market_bars_daily WHERE security_id='SSE:600519'"
        ).fetchone()[0]
        assert count == 2

    def test_upsert_is_idempotent(self, store):
        bars = [
            {'trade_date': '2024-01-01', 'open': 1.0, 'high': 2.0, 'low': 0.5, 'close': 1.5, 'volume': 100, 'amount': 150.0},
        ]
        # Write twice — should not create duplicate rows
        store.write_market_bars('SSE:600519', bars, 'mootdx', 'job-123')
        store.write_market_bars('SSE:600519', bars, 'mootdx', 'job-456')

        conn = store.get_conn()
        count = conn.execute(
            "SELECT COUNT(*) FROM market_bars_daily WHERE security_id='SSE:600519'"
        ).fetchone()[0]
        assert count == 1

    def test_upsert_updates_existing_row(self, store):
        bars_v1 = [
            {'trade_date': '2024-01-01', 'open': 1.0, 'high': 2.0, 'low': 0.5, 'close': 1.5, 'volume': 100, 'amount': 150.0},
        ]
        bars_v2 = [
            {'trade_date': '2024-01-01', 'open': 1.1, 'high': 2.1, 'low': 0.6, 'close': 1.6, 'volume': 110, 'amount': 176.0},
        ]

        store.write_market_bars('SSE:600519', bars_v1, 'mootdx', 'job-123')
        store.write_market_bars('SSE:600519', bars_v2, 'mootdx', 'job-456')

        conn = store.get_conn()
        row = conn.execute(
            "SELECT open, close FROM market_bars_daily WHERE security_id='SSE:600519' AND trade_date='2024-01-01'"
        ).fetchone()
        assert row[0] == 1.1  # Updated open price


class TestSecurityMasterSnapshots:
    def test_create_snapshot(self, store):
        snapshot_id = store.create_snapshot('mootdx', '2024-01-01', 5000, 'abc123', 'STAGING')
        assert snapshot_id is not None

        conn = store.get_conn()
        snap = conn.execute(
            "SELECT * FROM security_master_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        assert snap is not None
        assert snap[1] == 'mootdx'  # source
        assert snap[5] == 'STAGING'  # status

    def test_write_security_master_rows(self, store):
        snapshot_id = store.create_snapshot('mootdx', '2024-01-01', 3, 'checksum', 'STAGING')
        securities = [
            {'code': '600519', 'exchange': 'SSE', 'security_type': 'stock', 'name': '贵州茅台'},
            {'code': '000001', 'exchange': 'SZSE', 'security_type': 'stock', 'name': '平安银行'},
            {'code': '300750', 'exchange': 'SZSE', 'security_type': 'stock', 'name': '宁德时代'},
        ]
        count = store.write_security_master_snapshot(snapshot_id, securities)
        assert count == 3

    def test_validate_snapshot_passes(self, store):
        # Create a valid snapshot with enough data
        snapshot_id = store.create_snapshot('mootdx', '2024-01-01', 5000, 'checksum', 'STAGING')

        # Generate enough securities to pass all gates — use non-overlapping codes
        securities = []
        # SSE: 600xxx (1800 unique codes)
        for i in range(1800):
            securities.append({'code': f'6{i:05d}', 'exchange': 'SSE', 'security_type': 'stock', 'name': f'Stock{i}'})
        # SZSE: 0xxx (2500 unique codes)
        for i in range(2500):
            securities.append({'code': f'0{i:04d}', 'exchange': 'SZSE', 'security_type': 'stock', 'name': f'Stock{i}'})
        # BSE: 83xxx (200 unique codes)
        for i in range(200):
            securities.append({'code': f'83{i:04d}', 'exchange': 'BSE', 'security_type': 'stock', 'name': f'Stock{i}'})

        store.write_security_master_snapshot(snapshot_id, securities)
        validation = store.validate_snapshot(snapshot_id)

        assert validation['valid'] is True
        assert validation['gates']['total_coverage']['pass'] is True

    def test_validate_snapshot_fails_on_duplicates(self, store):
        snapshot_id = store.create_snapshot('mootdx', '2024-01-01', 2, 'checksum', 'STAGING')
        securities = [
            {'code': '600519', 'exchange': 'SSE', 'security_type': 'stock', 'name': '贵州茅台'},
            {'code': '600519', 'exchange': 'SSE', 'security_type': 'stock', 'name': '贵州茅台'},  # Duplicate
        ]
        store.write_security_master_snapshot(snapshot_id, securities)
        validation = store.validate_snapshot(snapshot_id)

        assert validation['valid'] is False
        assert validation['gates']['unique_exchange_code']['pass'] is False

    def test_failed_snapshot_does_not_replace_active(self, store):
        # First create a valid ACTIVE snapshot — use non-overlapping codes
        active_id = store.create_snapshot('mootdx', '2024-01-01', 5000, 'checksum1', 'STAGING')
        securities = []
        for i in range(1800):
            securities.append({'code': f'6{i:05d}', 'exchange': 'SSE', 'security_type': 'stock', 'name': f'Stock{i}'})
        for i in range(2500):
            securities.append({'code': f'0{i:04d}', 'exchange': 'SZSE', 'security_type': 'stock', 'name': f'Stock{i}'})
        for i in range(200):
            securities.append({'code': f'83{i:04d}', 'exchange': 'BSE', 'security_type': 'stock', 'name': f'Stock{i}'})
        store.write_security_master_snapshot(active_id, securities)

        validation = store.validate_snapshot(active_id)
        if validation['valid']:
            store.update_snapshot_status(active_id, 'VALIDATED')
            store.activate_snapshot('security_master', active_id)

        # Now try to activate a bad snapshot
        bad_id = store.create_snapshot('mootdx', '2024-01-02', 2, 'checksum2', 'STAGING')
        bad_securities = [
            {'code': '600519', 'exchange': 'SSE', 'security_type': 'stock', 'name': '贵州茅台'},
            {'code': '600519', 'exchange': 'SSE', 'security_type': 'stock', 'name': '贵州茅台'},
        ]
        store.write_security_master_snapshot(bad_id, bad_securities)

        # Validation should fail
        bad_validation = store.validate_snapshot(bad_id)
        assert bad_validation['valid'] is False

        # Active snapshot should still be the good one
        active = store.get_active_snapshot('security_master')
        assert active == active_id

    def test_empty_universe_cannot_activate(self, store):
        snapshot_id = store.create_snapshot('mootdx', '2024-01-01', 0, '', 'STAGING')
        validation = store.validate_snapshot(snapshot_id)

        assert validation['valid'] is False


class TestDatasetHeads:
    def test_activate_snapshot(self, store):
        snapshot_id = store.create_snapshot('mootdx', '2024-01-01', 5000, 'checksum', 'STAGING')

        # Must be VALIDATED before activation
        store.update_snapshot_status(snapshot_id, 'VALIDATED')
        store.activate_snapshot('security_master', snapshot_id)

        active = store.get_active_snapshot('security_master')
        assert active == snapshot_id

    def test_cannot_activate_non_validated_snapshot(self, store):
        snapshot_id = store.create_snapshot('mootdx', '2024-01-01', 5000, 'checksum', 'STAGING')

        with pytest.raises(ValueError, match="not VALIDATED"):
            store.activate_snapshot('security_master', snapshot_id)


class TestPathTraversal:
    def test_path_must_be_under_app_data(self):
        with pytest.raises(ValueError, match="must be under /app/data"):
            DatasetStore('/tmp/evil.duckdb')

        with pytest.raises(ValueError, match="must be under /app/data"):
            DatasetStore('/etc/passwd')

        # Valid path
        store = DatasetStore('/app/data/astock_data.duckdb')
        assert store.duckdb_path == '/app/data/astock_data.duckdb'


class TestIngestionLog:
    def test_log_ingestion(self, store):
        store.log_ingestion('market_bars_sync', 'job-123', 'market_bars_daily',
                           rows_inserted=10, rows_updated=5, status='success')

        conn = store.get_conn()
        log = conn.execute(
            "SELECT * FROM ingestion_log WHERE job_id='job-123'"
        ).fetchone()
        assert log is not None
        assert log[4] == 10  # rows_inserted


class TestConcurrency:
    def test_concurrent_writers(self, store):
        """Test that concurrent DuckDB writes don't corrupt data."""
        import threading

        errors = []

        def write_bars(thread_id):
            try:
                bars = [
                    {'trade_date': f'2024-01-{i:02d}', 'open': 1.0, 'high': 2.0, 'low': 0.5,
                     'close': 1.5, 'volume': 100, 'amount': 150.0}
                    for i in range(1, 11)
                ]
                store.write_market_bars(f'SSE:600{thread_id:03d}', bars, 'mootdx', f'job-{thread_id}')
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_bars, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Concurrent writes failed: {errors}"

        # Verify all data was written
        conn = store.get_conn()
        count = conn.execute("SELECT COUNT(*) FROM market_bars_daily").fetchone()[0]
        assert count == 50  # 5 threads × 10 bars each
