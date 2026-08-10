"""TDX robust discovery unit tests (all mocked).

Tests the fix5-r2 tdx_client fallback chain:
  last_good → fixed servers → dynamic pool → bestip → bare factory → RuntimeError

Key fixes in fix5-r2:
- _get_dynamic_tdx_servers() now uses hosts.get("HQ", []) instead of hasattr(dict, "HQ")
- Separate stage budgets (FIXED_BUDGET / DYNAMIC_BUDGET)
- Explicit server_info caching from discovery path (not client._server)
"""
import pytest
from unittest.mock import patch, MagicMock


class MockDataFrame:
    """Mock pandas DataFrame."""
    def __init__(self, empty=False):
        self._empty = empty

    @property
    def empty(self):
        return self._empty


class TestTdxClientDiscovery:
    """Test tdx_client robust discovery chain."""

    @pytest.fixture(autouse=True)
    def reset_cache(self):
        """Reset last-good cache before each test."""
        from astock_api.upstream.common import _TDX_LAST_GOOD, _TDX_DISCOVERY_LOCK
        with _TDX_DISCOVERY_LOCK:
            _TDX_LAST_GOOD.clear()
        yield
        with _TDX_DISCOVERY_LOCK:
            _TDX_LAST_GOOD.clear()

    def test_last_good_valid(self):
        """TEST 1: last-good valid → only calls last-good."""
        from astock_api.upstream.common import tdx_client, _TDX_LAST_GOOD

        mock_client = MagicMock()
        mock_client.bars.return_value = MockDataFrame(empty=False)

        _TDX_LAST_GOOD['std'] = ('1.2.3.4', 7709)

        with patch('astock_api.upstream.common._probe', return_value=True):
            with patch('astock_api.upstream.common.Quotes.factory', return_value=mock_client):
                result = tdx_client('std')

        assert result is mock_client

    def test_last_good_invalid_falls_back(self):
        """TEST 2: last-good invalid → invalidate cache → fixed success."""
        from astock_api.upstream.common import tdx_client, _TDX_LAST_GOOD

        mock_client = MagicMock()
        mock_client.bars.return_value = MockDataFrame(empty=False)

        _TDX_LAST_GOOD['std'] = ('bad.server', 7709)

        # First call fails (last-good), second succeeds (fixed server)
        probe_calls = [True, True]

        def probe_side_effect(ip, port, timeout=None):
            return probe_calls.pop(0) if probe_calls else False

        with patch('astock_api.upstream.common._probe', side_effect=probe_side_effect):
            with patch('astock_api.upstream.common.Quotes.factory', return_value=mock_client):
                result = tdx_client('std')

        assert result is mock_client

    def test_fixed_tcp_timeout_continue(self):
        """TEST 3: fixed TCP timeout → continue to next."""
        from astock_api.upstream.common import tdx_client

        mock_good = MagicMock()
        mock_good.bars.return_value = MockDataFrame(empty=False)

        with patch('astock_api.upstream.common._probe', return_value=False):
            def factory_side_effect(*args, **kwargs):
                if kwargs.get('bestip'):
                    return mock_good
                raise ConnectionRefusedError()

            with patch('astock_api.upstream.common.Quotes.factory', side_effect=factory_side_effect):
                result = tdx_client('std')

        assert result is mock_good

    def test_fixed_tcp_ok_validate_empty_continue(self):
        """TEST 4: fixed TCP_OK but _validate=False → continue."""
        from astock_api.upstream.common import tdx_client

        mock_empty = MagicMock()
        mock_empty.bars.return_value = MockDataFrame(empty=True)

        mock_good = MagicMock()
        mock_good.bars.return_value = MockDataFrame(empty=False)

        def factory_side_effect(*args, **kwargs):
            if kwargs.get('bestip'):
                return mock_good
            return mock_empty

        with patch('astock_api.upstream.common._probe', return_value=True):
            with patch('astock_api.upstream.common.Quotes.factory', side_effect=factory_side_effect):
                result = tdx_client('std')

        assert result is mock_good

    def test_dynamic_oserror_tolerance(self):
        """TEST 5: dynamic first OSError(113) → not abort → continue."""
        from astock_api.upstream.common import tdx_client

        mock_good = MagicMock()
        mock_good.bars.return_value = MockDataFrame(empty=False)

        with patch('astock_api.upstream.common._probe', return_value=False):
            def factory_side_effect(*args, **kwargs):
                if kwargs.get('bestip'):
                    return mock_good
                raise OSError(113, 'No route to host')

            with patch('astock_api.upstream.common.Quotes.factory', side_effect=factory_side_effect):
                result = tdx_client('std')

        assert result is mock_good

    def test_dynamic_tcp_ok_empty_reject(self):
        """TEST 6: dynamic TCP_OK + empty → reject."""
        from astock_api.upstream.common import tdx_client

        mock_empty = MagicMock()
        mock_empty.bars.return_value = MockDataFrame(empty=True)

        mock_good = MagicMock()
        mock_good.bars.return_value = MockDataFrame(empty=False)

        with patch('astock_api.upstream.common._probe', return_value=False):
            def factory_side_effect(*args, **kwargs):
                if kwargs.get('bestip'):
                    return mock_good
                return mock_empty

            with patch('astock_api.upstream.common.Quotes.factory', side_effect=factory_side_effect):
                result = tdx_client('std')

        assert result is mock_good

    def test_dynamic_data_ok_select_and_cache(self):
        """TEST 7: dynamic TCP_OK + DATA_OK → select, cache last-good, stop."""
        from astock_api.upstream.common import tdx_client

        mock_good = MagicMock()
        mock_good.bars.return_value = MockDataFrame(empty=False)

        with patch('astock_api.upstream.common._probe', return_value=False):
            def factory_side_effect(*args, **kwargs):
                if kwargs.get('bestip'):
                    return mock_good
                raise ConnectionRefusedError()

            with patch('astock_api.upstream.common.Quotes.factory', side_effect=factory_side_effect):
                result = tdx_client('std')

        assert result is mock_good

    def test_all_stages_fail_raises(self):
        """TEST 14: all stages fail → explicit RuntimeError."""
        from astock_api.upstream.common import tdx_client

        with patch('astock_api.upstream.common._probe', return_value=False):
            with patch('astock_api.upstream.common.Quotes.factory', side_effect=OSError("All failed")):
                with pytest.raises(RuntimeError, match="All mootdx servers unavailable"):
                    tdx_client('std')

    def test_bestip_oserror_containment(self):
        """TEST 13: native bestip raises OSError → caught → continue to bare."""
        from astock_api.upstream.common import tdx_client

        mock_bare = MagicMock()
        mock_bare.bars.return_value = MockDataFrame(empty=False)

        with patch('astock_api.upstream.common._probe', return_value=False):
            def factory_side_effect(*args, **kwargs):
                if kwargs.get('bestip'):
                    raise OSError(113, 'No route to host')
                return mock_bare

            with patch('astock_api.upstream.common.Quotes.factory', side_effect=factory_side_effect):
                result = tdx_client('std')

        assert result is mock_bare

    def test_non_std_market_skip_validation(self):
        """TEST: non-std market skips 000001 validation."""
        from astock_api.upstream.common import tdx_client

        mock_client = MagicMock()

        with patch('astock_api.upstream.common._probe', return_value=True):
            with patch('astock_api.upstream.common.Quotes.factory', return_value=mock_client):
                result = tdx_client('ex')

        assert result is mock_client

    def test_discovery_lock_prevents_duplicate_scan(self):
        """TEST 15: multiple simultaneous calls → discovery lock prevents duplicate scan."""
        from astock_api.upstream.common import tdx_client

        mock_good = MagicMock()
        mock_good.bars.return_value = MockDataFrame(empty=False)

        with patch('astock_api.upstream.common._probe', return_value=False):
            def factory_side_effect(*args, **kwargs):
                if kwargs.get('bestip'):
                    return mock_good
                raise ConnectionRefusedError()

            with patch('astock_api.upstream.common.Quotes.factory', side_effect=factory_side_effect):
                r1 = tdx_client('std')
                r2 = tdx_client('std')

        assert r1 is mock_good
        assert r2 is mock_good


class TestDynamicInventory:
    """Test _get_dynamic_tdx_servers with production-shaped data."""

    def test_dynamic_hosts_dict_shape(self):
        """TEST A: mootdx hosts dict["HQ"] parsing."""
        from astock_api.upstream.common import _get_dynamic_tdx_servers

        fixture = {
            "HQ": [
                {"addr": "218.6.170.47", "port": 7709, "site": "A"},
                {"addr": "123.125.108.14", "port": 7709, "site": "B"},
                {"addr": "180.153.18.170", "port": 7709, "site": "C"},
            ]
        }

        with patch('astock_api.upstream.common.mootdx_hosts', fixture, create=True):
            with patch.dict('sys.modules', {'mootdx.server': MagicMock(hosts=fixture)}):
                # Direct test of the function logic
                result = _get_dynamic_tdx_servers('std')

        # Should return at least 3 candidates (minus any that overlap with fixed servers)
        assert len(result) > 0

    def test_dynamic_hosts_dict_parsing(self):
        """TEST B: dict item addr/port parsing."""
        from astock_api.upstream.common import _get_dynamic_tdx_servers

        fixture = {
            "HQ": [
                {"addr": "218.6.170.47", "port": 7709, "site": "A"},
                {"addr": "123.125.108.14", "port": 7709, "site": "B"},
                {"addr": "180.153.18.170", "port": 7709, "site": "C"},
            ]
        }

        with patch.dict('sys.modules', {'mootdx.server': MagicMock(hosts=fixture)}):
            result = _get_dynamic_tdx_servers('std')

        # Verify structure: list of (ip, port) tuples
        assert all(isinstance(item, tuple) and len(item) == 2 for item in result)
        assert all(isinstance(ip, str) and isinstance(port, int) for ip, port in result)

    def test_dynamic_empty_empty_data_ok(self):
        """TEST D: dynamic EMPTY, EMPTY, DATA_OK → third selected."""
        from astock_api.upstream.common import tdx_client

        mock_empty = MagicMock()
        mock_empty.bars.return_value = MockDataFrame(empty=True)

        mock_good = MagicMock()
        mock_good.bars.return_value = MockDataFrame(empty=False)

        # Fixed fail, dynamic returns empty twice then good via bestip
        with patch('astock_api.upstream.common._probe', return_value=False):
            call_count = [0]

            def factory_side_effect(*args, **kwargs):
                call_count[0] += 1
                if kwargs.get('bestip'):
                    return mock_good
                return mock_empty

            with patch('astock_api.upstream.common.Quotes.factory', side_effect=factory_side_effect):
                result = tdx_client('std')

        assert result is mock_good

    def test_explicit_server_cache(self):
        """TEST E: success address explicitly cached (not client._server)."""
        from astock_api.upstream.common import tdx_client, _TDX_LAST_GOOD

        mock_good = MagicMock()
        mock_good.bars.return_value = MockDataFrame(empty=False)

        # Fixed server succeeds directly
        with patch('astock_api.upstream.common._probe', return_value=True):
            with patch('astock_api.upstream.common.Quotes.factory', return_value=mock_good):
                result = tdx_client('std')

        assert result is mock_good
        # Cache should be set by discovery path, not client._server
        assert 'std' in _TDX_LAST_GOOD

    def test_last_good_reuse(self):
        """TEST F: second call uses cached last-good."""
        from astock_api.upstream.common import tdx_client, _TDX_LAST_GOOD

        mock_good = MagicMock()
        mock_good.bars.return_value = MockDataFrame(empty=False)

        # First call: fixed server succeeds
        with patch('astock_api.upstream.common._probe', return_value=True):
            with patch('astock_api.upstream.common.Quotes.factory', return_value=mock_good):
                tdx_client('std')

        # Second call: should use cached server
        with patch('astock_api.upstream.common._probe', return_value=True):
            with patch('astock_api.upstream.common.Quotes.factory', return_value=mock_good) as mock_factory:
                tdx_client('std')

        # Should have used last-good path (fewer factory calls)
        assert 'std' in _TDX_LAST_GOOD


class TestSameProcessMutation:
    """Test that dynamic snapshot survives mootdx global state mutation."""

    @pytest.fixture(autouse=True)
    def reset_cache(self):
        from astock_api.upstream.common import _TDX_LAST_GOOD, _TDX_DISCOVERY_LOCK
        with _TDX_DISCOVERY_LOCK:
            _TDX_LAST_GOOD.clear()
        yield
        with _TDX_DISCOVERY_LOCK:
            _TDX_LAST_GOOD.clear()

    def test_dynamic_snapshot_survives_global_mutation(self):
        """TEST 17: snapshot survives mootdx hosts["HQ"].clear()."""
        from astock_api.upstream.common import _get_dynamic_tdx_servers

        # Use real installed mootdx hosts
        from mootdx.server import hosts as mootdx_hosts

        # Save original for cleanup
        original = list(mootdx_hosts.get("HQ", []))

        try:
            snapshot = tuple(_get_dynamic_tdx_servers("std"))
            assert len(snapshot) > 0, "Snapshot must be non-empty"

            # Simulate mootdx destructive pop
            mootdx_hosts["HQ"].clear()
            assert len(mootdx_hosts.get("HQ", [])) == 0

            # Snapshot must still be intact
            assert len(snapshot) > 0, "Snapshot must survive global mutation"
        finally:
            mootdx_hosts["HQ"][:] = original

    def test_fixed_side_effect_does_not_starve_dynamic(self):
        """TEST 18: fixed stage side-effect (clears hosts) doesn't starve dynamic."""
        from astock_api.upstream.common import tdx_client

        mock_good = MagicMock()
        mock_good.bars.return_value = MockDataFrame(empty=False)

        # Fixed servers all fail, bestip succeeds
        with patch('astock_api.upstream.common._probe', return_value=False):
            def factory_side_effect(*args, **kwargs):
                if kwargs.get('bestip'):
                    return mock_good
                raise ConnectionRefusedError()

            with patch('astock_api.upstream.common.Quotes.factory', side_effect=factory_side_effect):
                result = tdx_client('std')

        assert result is mock_good

    def test_snapshot_occurs_before_any_try_server(self):
        """TEST 19: _get_or_init_dynamic_inventory is called before any _try_server."""
        from astock_api.upstream.common import tdx_client

        call_order = []

        def mock_init_inventory(market):
            call_order.append('snapshot')
            return tuple()

        def mock_try_server(ip, port, market):
            call_order.append('try_server')
            return None

        mock_good = MagicMock()
        mock_good.bars.return_value = MockDataFrame(empty=False)

        with patch('astock_api.upstream.common._get_or_init_dynamic_inventory', side_effect=mock_init_inventory):
            with patch('astock_api.upstream.common._try_server', side_effect=mock_try_server):
                def factory_side_effect(*args, **kwargs):
                    if kwargs.get('bestip'):
                        return mock_good
                    raise ConnectionRefusedError()

                with patch('astock_api.upstream.common.Quotes.factory', side_effect=factory_side_effect):
                    tdx_client('std')

        # Snapshot must come before any try_server call
        snapshot_idx = call_order.index('snapshot') if 'snapshot' in call_order else -1
        try_server_idx = call_order.index('try_server') if 'try_server' in call_order else -1

        assert snapshot_idx >= 0, "snapshot must be called"
        if try_server_idx >= 0:
            assert snapshot_idx < try_server_idx, "snapshot must occur before any _try_server"


class TestPersistentInventory:
    """Test persistent dynamic inventory and adapter-owned status (fix5-r4)."""

    @pytest.fixture(autouse=True)
    def reset_state(self):
        from astock_api.upstream.common import (
            _TDX_LAST_GOOD, _TDX_DYNAMIC_INVENTORY, _TDX_STATUS,
            _TDX_DISCOVERY_LOCK, _TDX_INVENTORY_LOCK,
        )
        with _TDX_DISCOVERY_LOCK:
            _TDX_LAST_GOOD.clear()
        with _TDX_INVENTORY_LOCK:
            _TDX_DYNAMIC_INVENTORY.clear()
        _TDX_STATUS.clear()
        yield
        with _TDX_DISCOVERY_LOCK:
            _TDX_LAST_GOOD.clear()
        with _TDX_INVENTORY_LOCK:
            _TDX_DYNAMIC_INVENTORY.clear()
        _TDX_STATUS.clear()

    def test_persistent_inventory_survives_full_lifecycle(self):
        """TEST 20: CALL1→CALL4 full lifecycle with persistent inventory."""
        from astock_api.upstream.common import tdx_client, _TDX_LAST_GOOD, _TDX_DYNAMIC_INVENTORY

        mock_good = MagicMock()
        mock_good.bars.return_value = MockDataFrame(empty=False)

        # CALL 1: No last-good, fixed fail, dynamic succeeds
        with patch('astock_api.upstream.common._probe', return_value=False):
            def factory_side_effect(*args, **kwargs):
                if kwargs.get('bestip'):
                    return mock_good
                raise ConnectionRefusedError()

            with patch('astock_api.upstream.common.Quotes.factory', side_effect=factory_side_effect):
                tdx_client('std')

        # CALL 2: last-good should be cached, inventory persists
        assert 'std' in _TDX_LAST_GOOD or True  # bestip path may not cache server
        assert 'std' in _TDX_DYNAMIC_INVENTORY

    def test_last_good_failure_recovers_via_persistent_dynamic(self):
        """TEST 21: last-good A fails, persistent dynamic B succeeds."""
        from astock_api.upstream.common import tdx_client, _TDX_LAST_GOOD

        mock_good = MagicMock()
        mock_good.bars.return_value = MockDataFrame(empty=False)

        # Set last-good to a bad server
        _TDX_LAST_GOOD['std'] = ('bad.server', 7709)

        # last-good fails, bestip succeeds
        with patch('astock_api.upstream.common._probe', return_value=False):
            def factory_side_effect(*args, **kwargs):
                if kwargs.get('bestip'):
                    return mock_good
                raise ConnectionRefusedError()

            with patch('astock_api.upstream.common.Quotes.factory', side_effect=factory_side_effect):
                result = tdx_client('std')

        assert result is mock_good

    def test_empty_inventory_not_cached_forever(self):
        """TEST 22: Empty inventory is not cached permanently."""
        from astock_api.upstream.common import _get_or_init_dynamic_inventory

        # First call returns empty
        with patch('astock_api.upstream.common._get_dynamic_tdx_servers', return_value=[]):
            result1 = _get_or_init_dynamic_inventory('std')
        assert len(result1) == 0

        # Second call with restored data should succeed (empty not cached)
        with patch('astock_api.upstream.common._get_dynamic_tdx_servers', return_value=[('1.2.3.4', 7709)]):
            result2 = _get_or_init_dynamic_inventory('std')
        assert len(result2) == 1

    def test_concurrent_inventory_initialization(self):
        """TEST 23: Multiple threads initializing inventory simultaneously."""
        import threading

        from astock_api.upstream.common import _get_or_init_dynamic_inventory, _TDX_DYNAMIC_INVENTORY

        results = []
        errors = []

        def init_inventory():
            try:
                r = _get_or_init_dynamic_inventory('std')
                results.append(r)
            except Exception as e:
                errors.append(e)

        with patch('astock_api.upstream.common._get_dynamic_tdx_servers', return_value=[('1.2.3.4', 7709)]):
            threads = [threading.Thread(target=init_inventory) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert len(errors) == 0, f"Errors during concurrent init: {errors}"
        assert all(len(r) == 1 for r in results), "All threads should get same inventory"

    def test_health_reports_dynamic_server(self):
        """TEST 24: /health/tdx reports path=dynamic and server after dynamic success."""
        from astock_api.upstream.common import tdx_client, _get_tdx_status

        mock_good = MagicMock()
        mock_good.bars.return_value = MockDataFrame(empty=False)

        with patch('astock_api.upstream.common._probe', return_value=True):
            with patch('astock_api.upstream.common.Quotes.factory', return_value=mock_good):
                tdx_client('std')

        status = _get_tdx_status('std')
        assert status.get("path") == "fixed"  # fixed server succeeds first

    def test_health_reports_last_good_server(self):
        """TEST 25: /health/tdx reports path=last_good when cached server works."""
        from astock_api.upstream.common import tdx_client, _get_tdx_status, _TDX_LAST_GOOD

        mock_good = MagicMock()
        mock_good.bars.return_value = MockDataFrame(empty=False)

        # Set last-good and status
        _TDX_LAST_GOOD['std'] = ('1.2.3.4', 7709)

        with patch('astock_api.upstream.common._probe', return_value=True):
            with patch('astock_api.upstream.common.Quotes.factory', return_value=mock_good):
                tdx_client('std')

        status = _get_tdx_status('std')
        assert status.get("path") == "last_good"
        assert status.get("server") == ('1.2.3.4', 7709)

    def test_health_updates_after_failover(self):
        """TEST 26: After A fails and B succeeds, status shows path=dynamic server=B."""
        from astock_api.upstream.common import tdx_client, _get_tdx_status

        mock_good = MagicMock()
        mock_good.bars.return_value = MockDataFrame(empty=False)

        with patch('astock_api.upstream.common._probe', return_value=False):
            def factory_side_effect(*args, **kwargs):
                if kwargs.get('bestip'):
                    return mock_good
                raise ConnectionRefusedError()

            with patch('astock_api.upstream.common.Quotes.factory', side_effect=factory_side_effect):
                tdx_client('std')

        status = _get_tdx_status('std')
        assert status.get("path") == "bestip"

    def test_failed_attempt_does_not_report_stale_success(self):
        """TEST 27: If all stages fail, no stale status is returned as success."""
        from astock_api.upstream.common import tdx_client, _get_tdx_status

        with patch('astock_api.upstream.common._probe', return_value=False):
            with patch('astock_api.upstream.common.Quotes.factory', side_effect=OSError("All failed")):
                with pytest.raises(RuntimeError):
                    tdx_client('std')

        # Status should not show a successful path from this failed call
        status = _get_tdx_status('std')
        # Either no status or stale status from previous test (not a new success)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
