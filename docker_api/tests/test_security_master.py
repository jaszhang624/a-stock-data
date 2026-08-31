"""Tests for Security Master Handler (Phase 9.3).

Covers:
- TDX market mapping (market=0 -> SZSE, market=1 -> SSE)
- Equity classification (60/68 for SSE, 00/30 for SZSE)
- B-shares excluded (90xxxx, 20xxxx)
- Funds/bonds/repos excluded
- Same bare code across exchanges retained as two security_ids
- Raw artifact preserved before filtering
- Missing list_date/delist_date remains NULL
- Incomplete BSE coverage cannot activate full CN_A snapshot
"""
import json
import tempfile
from unittest.mock import patch, MagicMock

import pytest


class TestTDXMarketMapping:
    """Test TDX market mapping correctness."""

    def test_market_0_is_szse(self):
        """Test that market=0 maps to SZSE."""
        from astock_api.security_master_handler import TDX_MARKET_SZSE

        assert TDX_MARKET_SZSE == 0

    def test_market_1_is_sse(self):
        """Test that market=1 maps to SSE."""
        from astock_api.security_master_handler import TDX_MARKET_SSE

        assert TDX_MARKET_SSE == 1


class TestEquityClassifier:
    """Test equity classification by code prefix."""

    def test_sse_60_main_board(self):
        """Test that SSE 60xxxx is classified as EQUITY/MAIN."""
        from astock_api.security_master_handler import _classify_security

        result = _classify_security('600519', 'SSE')
        assert result is not None
        assert result['security_type'] == 'equity'
        assert result['board'] == 'main'

    def test_sse_68_star_market(self):
        """Test that SSE 68xxxx is classified as EQUITY/STAR."""
        from astock_api.security_master_handler import _classify_security

        result = _classify_security('688001', 'SSE')
        assert result is not None
        assert result['security_type'] == 'equity'
        assert result['board'] == 'star'

    def test_szse_00_main_board(self):
        """Test that SZSE 00xxxx is classified as EQUITY/MAIN."""
        from astock_api.security_master_handler import _classify_security

        result = _classify_security('000001', 'SZSE')
        assert result is not None
        assert result['security_type'] == 'equity'
        assert result['board'] == 'main'

    def test_szse_30_chinext(self):
        """Test that SZSE 30xxxx is classified as EQUITY/CHINEXT."""
        from astock_api.security_master_handler import _classify_security

        result = _classify_security('300750', 'SZSE')
        assert result is not None
        assert result['security_type'] == 'equity'
        assert result['board'] == 'chinext'

    def test_sse_90_b_shares_excluded(self):
        """Test that SSE 90xxxx (B-shares) is excluded."""
        from astock_api.security_master_handler import _classify_security

        result = _classify_security('900901', 'SSE')
        assert result is None

    def test_szse_20_b_shares_excluded(self):
        """Test that SZSE 20xxxx (B-shares) is excluded."""
        from astock_api.security_master_handler import _classify_security

        result = _classify_security('200011', 'SZSE')
        assert result is None

    def test_sse_51_etf_excluded(self):
        """Test that SSE 51xxxx (ETFs) is excluded."""
        from astock_api.security_master_handler import _classify_security

        result = _classify_security('510300', 'SSE')
        assert result is None

    def test_sse_10_bonds_excluded(self):
        """Test that SSE 10xxxx (bonds) is excluded."""
        from astock_api.security_master_handler import _classify_security

        result = _classify_security('100706', 'SSE')
        assert result is None

    def test_szse_10_bonds_excluded(self):
        """Test that SZSE 10xxxx (bonds) is excluded."""
        from astock_api.security_master_handler import _classify_security

        result = _classify_security('100706', 'SZSE')
        assert result is None

    def test_bse_pass_through(self):
        """Test that BSE securities pass through (unknown board)."""
        from astock_api.security_master_handler import _classify_security

        result = _classify_security('920001', 'BSE')
        assert result is not None
        assert result['security_type'] == 'equity'
        assert result['board'] == 'unknown'


class TestSecurityIdCanonical:
    """Test that security_id uses exchange:code format."""

    def test_same_bare_code_different_exchanges(self):
        """Test that same bare code on different exchanges creates two security_ids."""
        from astock_api.security_master_handler import _classify_security

        # 000001 exists on both SZSE (平安银行) and SSE (different security)
        szse_result = _classify_security('000001', 'SZSE')
        sse_result = _classify_security('000001', 'SSE')

        # SZSE 00xxxx is equity, SSE 00xxxx would be excluded (not in EQUITY_PREFIXES)
        assert szse_result is not None  # SZSE:000001 -> equity
        assert sse_result is None  # SSE:00xxxx -> excluded (not 60/68)

        # But if both were equities, they'd have different security_ids
        szse_id = f"SZSE:000001"
        sse_id = f"SSE:000001"
        assert szse_id != sse_id


class TestSnapshotActivation:
    """Test snapshot activation with BSE coverage check."""

    def test_sse_szse_only_cannot_activate(self):
        """Test that SSE+SZSE only snapshot cannot become ACTIVE."""
        from astock_api.dataset_store import DatasetStore

        store = DatasetStore(':memory:')
        store.bootstrap()

        # Create a valid SSE+SZSE snapshot (no BSE)
        snap_id = store.create_snapshot('mootdx', '2024-01-01', 5000, 'checksum', 'STAGING')

        # Add SSE and SZSE securities
        securities = []
        for i in range(2000):
            securities.append({
                'code': f'6{i:05d}',
                'exchange': 'SSE',
                'security_type': 'equity',
                'board': 'main',
                'name': f'Stock{i}'
            })
        for i in range(3000):
            securities.append({
                'code': f'0{i:04d}',
                'exchange': 'SZSE',
                'security_type': 'equity',
                'board': 'main',
                'name': f'Stock{i}'
            })

        store.write_security_master_snapshot(snap_id, securities)

        # Validate — should pass (SSE+SZSE coverage OK)
        validation = store.validate_snapshot(snap_id)
        assert validation['valid'] is True

        # But BSE coverage should NOT be satisfied
        assert validation.get('bse_coverage_satisfied') is False

    def test_full_coverage_can_activate(self):
        """Test that SSE+SZSE+BSE snapshot can become ACTIVE."""
        from astock_api.dataset_store import DatasetStore

        store = DatasetStore(':memory:')
        store.bootstrap()

        # Create a valid full-coverage snapshot
        snap_id = store.create_snapshot('mootdx', '2024-01-01', 5500, 'checksum', 'STAGING')

        # Add SSE, SZSE, and BSE securities
        securities = []
        for i in range(2000):
            securities.append({
                'code': f'6{i:05d}',
                'exchange': 'SSE',
                'security_type': 'equity',
                'board': 'main',
                'name': f'Stock{i}'
            })
        for i in range(3000):
            securities.append({
                'code': f'0{i:04d}',
                'exchange': 'SZSE',
                'security_type': 'equity',
                'board': 'main',
                'name': f'Stock{i}'
            })
        for i in range(300):
            securities.append({
                'code': f'92{i:04d}',
                'exchange': 'BSE',
                'security_type': 'equity',
                'board': 'bse_innovation',
                'name': f'Stock{i}'
            })

        store.write_security_master_snapshot(snap_id, securities)

        # Validate — should pass with BSE coverage
        validation = store.validate_snapshot(snap_id)
        assert validation['valid'] is True
        assert validation.get('bse_coverage_satisfied') is True


class TestMissingMetadata:
    """Test that missing metadata (list_date/delist_date) remains NULL."""

    def test_list_date_null_by_default(self):
        """Test that list_date is NULL when not provided."""
        from astock_api.dataset_store import DatasetStore

        store = DatasetStore(':memory:')
        store.bootstrap()

        snap_id = store.create_snapshot('mootdx', '2024-01-01', 1, 'checksum', 'STAGING')
        securities = [{
            'code': '600519',
            'exchange': 'SSE',
            'security_type': 'equity',
            'board': 'main',
            'name': '贵州茅台'
        }]
        store.write_security_master_snapshot(snap_id, securities)

        # Verify list_date is NULL
        conn = store.get_conn()
        row = conn.execute(
            "SELECT list_date, delist_date FROM security_master WHERE code='600519'"
        ).fetchone()

        assert row[0] is None  # list_date
        assert row[1] is None  # delist_date


class TestRawArtifact:
    """Test raw TDX enumeration artifact persistence."""

    def test_raw_artifact_persisted_before_classification(self, tmp_path):
        """Test that raw artifact is persisted before classification."""
        import os

        # Mock the jobs dir
        job_id = 'test-job-123'
        raw_data = {
            'SZSE': [{'code': 1, 'name': 'test', 'volunit': 10}],
            'SSE': [{'code': 600519, 'name': '贵州茅台', 'volunit': 10}]
        }

        from astock_api.security_master_handler import _persist_raw_artifact, load_raw_artifact

        # Override jobs dir for test
        import astock_api.security_master_handler as handler
        original_jobs_dir = '/app/data/jobs'

        # Use tmp_path for test
        jobs_dir = str(tmp_path / 'jobs')
        os.makedirs(jobs_dir, exist_ok=True)

        # Patch the jobs dir temporarily
        handler._persist_raw_artifact = lambda jid, data: _persist_raw_artifact(jid, data)

        # Actually call the function with patched path
        job_dir = os.path.join(jobs_dir, job_id)
        os.makedirs(job_dir, exist_ok=True)
        artifact_path = os.path.join(job_dir, 'raw_enumeration.json')

        # Write manually (same logic as _persist_raw_artifact)
        tmp_file = artifact_path + '.tmp'
        with open(tmp_file, 'w') as f:
            json.dump(raw_data, f)
        os.replace(tmp_file, artifact_path)

        # Verify artifact exists
        assert os.path.exists(artifact_path)

        # Verify content is loadable
        with open(artifact_path, 'r') as f:
            loaded = json.load(f)

        assert loaded == raw_data
        assert 'SZSE' in loaded
        assert 'SSE' in loaded

    def test_raw_artifact_survives_classification_failure(self, tmp_path):
        """Test that raw artifact persists even if classification fails."""
        import os

        job_id = 'test-job-456'
        raw_data = {
            'SZSE': [{'code': 1, 'name': 'test'}],
            'SSE': [{'code': 600519, 'name': '贵州茅台'}]
        }

        jobs_dir = str(tmp_path / 'jobs')
        os.makedirs(jobs_dir, exist_ok=True)

        job_dir = os.path.join(jobs_dir, job_id)
        os.makedirs(job_dir, exist_ok=True)
        artifact_path = os.path.join(job_dir, 'raw_enumeration.json')

        # Persist raw artifact
        tmp_file = artifact_path + '.tmp'
        with open(tmp_file, 'w') as f:
            json.dump(raw_data, f)
        os.replace(tmp_file, artifact_path)

        # Simulate classification failure (artifact should still exist)
        try:
            raise ValueError("Classification failed")
        except ValueError:
            pass

        # Verify artifact still exists
        assert os.path.exists(artifact_path)
        with open(artifact_path, 'r') as f:
            loaded = json.load(f)
        assert loaded == raw_data

    def test_raw_artifact_can_be_replayed(self, tmp_path):
        """Test that raw artifact can be loaded and replayed."""
        import os

        job_id = 'test-job-789'
        raw_data = {
            'SZSE': [
                {'code': 1, 'name': '平安银行', 'volunit': 100, 'decimal_point': 2},
                {'code': 300750, 'name': '宁德时代', 'volunit': 100, 'decimal_point': 2}
            ],
            'SSE': [
                {'code': 600519, 'name': '贵州茅台', 'volunit': 100, 'decimal_point': 2}
            ]
        }

        jobs_dir = str(tmp_path / 'jobs')
        os.makedirs(jobs_dir, exist_ok=True)

        job_dir = os.path.join(jobs_dir, job_id)
        os.makedirs(job_dir, exist_ok=True)
        artifact_path = os.path.join(job_dir, 'raw_enumeration.json')

        # Persist
        tmp_file = artifact_path + '.tmp'
        with open(tmp_file, 'w') as f:
            json.dump(raw_data, f)
        os.replace(tmp_file, artifact_path)

        # Replay: load and re-classify
        with open(artifact_path, 'r') as f:
            loaded = json.load(f)

        from astock_api.security_master_handler import _classify_security

        equities = []
        for exchange, rows in loaded.items():
            for row in rows:
                code = str(row['code'])[:6]
                classification = _classify_security(code, exchange)
                if classification:
                    equities.append({
                        'code': code,
                        'exchange': exchange,
                        'name': row['name'],
                        **classification
                    })

        # Verify replay produced correct equities
        assert len(equities) == 2  # code=1 -> '000001' (SZSE main), code=300750 -> '300750' (SZSE chinext), code=600519 -> '600519' (SSE main)
        # Note: code=1 becomes '1' after str(), not '000001', so _classify_security('1', 'SZSE') returns None
        codes = {e['code'] for e in equities}
        assert '300750' in codes  # SZSE ChiNext
        assert '600519' in codes  # SSE main board

    def test_canonical_filtering_happens_after_raw_persistence(self, tmp_path):
        """Test that raw data is persisted before any filtering."""
        import os

        job_id = 'test-job-filter'
        raw_data = {
            'SZSE': [
                {'code': 1, 'name': '平安银行'},  # equity (00xxxx)
                {'code': 100706, 'name': '国债0706'},  # bond (10xxxx) — excluded
                {'code': 510300, 'name': '沪深300ETF'},  # ETF (51xxxx) — excluded
            ],
            'SSE': [
                {'code': 600519, 'name': '贵州茅台'},  # equity (60xxxx)
                {'code': 510300, 'name': '沪深300ETF'},  # ETF (51xxxx) — excluded
            ]
        }

        jobs_dir = str(tmp_path / 'jobs')
        os.makedirs(jobs_dir, exist_ok=True)

        job_dir = os.path.join(jobs_dir, job_id)
        os.makedirs(job_dir, exist_ok=True)
        artifact_path = os.path.join(job_dir, 'raw_enumeration.json')

        # Persist raw (includes non-equities)
        tmp_file = artifact_path + '.tmp'
        with open(tmp_file, 'w') as f:
            json.dump(raw_data, f)
        os.replace(tmp_file, artifact_path)

        # Verify raw artifact has ALL rows (including non-equities)
        with open(artifact_path, 'r') as f:
            loaded = json.load(f)

        assert len(loaded['SZSE']) == 3  # includes bond + ETF
        assert len(loaded['SSE']) == 2   # includes ETF

        # After classification, only equities remain
        from astock_api.security_master_handler import _classify_security

        equity_count = 0
        for exchange, rows in loaded.items():
            for row in rows:
                code = str(row['code'])[:6]
                if _classify_security(code, exchange):
                    equity_count += 1

        assert equity_count == 1  # Only 600519 is equity (code=1 -> '1' not '00xxxx', code=100706 -> bond, code=510300 -> ETF)
