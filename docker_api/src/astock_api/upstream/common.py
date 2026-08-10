"""Upstream common utilities: tdx_client, get_prefix.

Extracted from a-stock-data SKILL.md - upstream version 3.6.0
Commit: 281fc69a0b733ffc6458fe2cf9f1ea56804aa886
Sync date: 2026-08-08

Changes in fix4 (TDX resilience):
- _TDX_SERVERS: Updated to 10 current servers from upstream V3.6.0
- _validate(): Use frequency=9, offset=1 (single bar) per upstream V3.4.1
- tdx_client(): Added bestip fallback + bare factory fallback per upstream V3.4.1
- _probe(): Use socket.create_connection per upstream

Changes in fix5 (Robust TDX discovery):
- Last-good server cache with per-market tracking
- Discovery lock to prevent concurrent scans
- Dynamic candidate pool from mootdx internals (lazy-loaded)
- Per-server error tolerance (OSError, empty response)
- Discovery budget limits (max candidates + time budget)
- Logging instead of print for observability

Changes in fix5-r2 (Production closure):
- FIX: _get_dynamic_tdx_servers() used hasattr(dict, "HQ") which always returns False
  Now uses hosts.get("HQ", []) for dict access + item.get("addr")/item.get("port")
- FIX: Separate stage budgets (FIXED_BUDGET / DYNAMIC_BUDGET) so fixed timeout doesn't starve dynamic
- FIX: Explicit (client, server_info, path) return from _discover() instead of client._server
- Added production-shaped dynamic inventory hard gate test

Changes in fix5-r3 (Same-process mutation fix):
- FIX: Snapshot dynamic inventory BEFORE any Quotes.factory() call
  mootdx 0.11.7 config.setup() → bestip(sync=False) destructively pops hosts["HQ"]
  Snapshot must occur before last_good/fixed stages that trigger Quotes.factory()

Changes in fix5-r4 (Persistent inventory + observability):
- Persistent dynamic inventory: survives mootdx global state mutation across calls
- Adapter-owned status tracking: path + server info for /health/tdx observability
- attempted_servers deduplication within single tdx_client() call
- /health/tdx now reports path and server from adapter status (not client._server)
"""
import logging
import socket
import threading
import time
from mootdx.quotes import Quotes

logger = logging.getLogger(__name__)

# Upstream V3.6.0 - 10 verified servers (2026-06)
_TDX_SERVERS = [
    ("180.153.18.170", 7709),  # business-validated node
    ('119.97.185.59', 7709), ('124.70.133.119', 7709), ('116.205.183.150', 7709),
    ('123.60.73.44', 7709),  ('116.205.163.254', 7709), ('121.36.225.169', 7709),
    ('123.60.70.228', 7709), ('124.71.9.153', 7709),    ('110.41.147.114', 7709),
    ('124.71.187.122', 7709),
]

SH_INDEX = ['000300', '000016', '000905', '000688', '000852', '000010']

# Last-good server cache: {market: (ip, port)}
_TDX_LAST_GOOD = {}

# Persistent dynamic inventory: {market: tuple of (ip, port)}
_TDX_DYNAMIC_INVENTORY = {}

# Discovery lock to prevent concurrent scans
_TDX_DISCOVERY_LOCK = threading.Lock()

# Inventory initialization lock (separate from discovery)
_TDX_INVENTORY_LOCK = threading.Lock()

# Adapter-owned status tracking: {market: {"path": str, "server": (ip,port)|None, "last_success": float}}
_TDX_STATUS = {}
_TDX_STATUS_LOCK = threading.Lock()

# Per-stage discovery budgets (seconds)
FIXED_DISCOVERY_TIME_BUDGET = 6.0
DYNAMIC_DISCOVERY_TIME_BUDGET = 15.0

# Discovery budget limits
MAX_DYNAMIC_CANDIDATES = 32
PROBE_TIMEOUT = 1.0  # per-server TCP probe timeout


def _probe(ip, port, timeout=PROBE_TIMEOUT):
    """TCP handshake probe (quick coarse filter). Note: success != usable; must pass _validate."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def _validate(client, market='std'):
    """Real data validation: bad servers may pass TCP handshake but return empty body.

    Uses '000001' as validation sample for std market only. Non-std markets skip
    validation to avoid false negatives (000001 is A-share specific).
    """
    if market != 'std':
        return True
    try:
        df = client.bars(symbol='000001', frequency=9, offset=1)
        return df is not None and not df.empty
    except Exception:
        return False


def _try_server(ip, port, market):
    """Try a single server with full validation. Returns Quotes client or None."""
    if not _probe(ip, port):
        return None
    try:
        c = Quotes.factory(market=market, server=(ip, port))
        if _validate(c, market):
            return c
    except Exception:
        pass
    return None


def _get_dynamic_tdx_servers(market):
    """Get dynamic candidate servers from mootdx internals (lazy-loaded).

    Returns a list of (ip, port) tuples, excluding fixed servers.
    Compatible with mootdx 0.11.7 where hosts is a dict: {"HQ": [{"addr":"...", "port":7709}, ...]}
    """
    try:
        from mootdx.server import hosts as mootdx_hosts

        # mootdx 0.11.7: hosts is a dict, not an object
        if isinstance(mootdx_hosts, dict):
            pool = mootdx_hosts.get("HQ", [])
        elif hasattr(mootdx_hosts, "HQ"):
            pool = mootdx_hosts.HQ
        else:
            return []

        if not pool:
            return []

        fixed_set = set(_TDX_SERVERS)
        candidates = []
        seen = set()

        for item in pool:
            if len(candidates) >= MAX_DYNAMIC_CANDIDATES:
                break

            ip = None
            port = 7709

            if isinstance(item, dict):
                ip = item.get("addr")
                port = item.get("port", 7709)
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                ip = item[0]
                port = item[1]

            if not ip:
                continue

            try:
                port = int(port)
            except (ValueError, TypeError):
                continue

            key = (str(ip), port)
            if key in seen or key in fixed_set:
                continue

            seen.add(key)
            candidates.append(key)

        return candidates
    except Exception:
        logger.debug("Failed to load dynamic TDX server pool")
        return []


def _get_or_init_dynamic_inventory(market):
    """Get or initialize persistent dynamic inventory for a market.

    Returns an immutable tuple of (ip, port) tuples.
    Empty inventory is NOT cached permanently — allows retry when mootdx hosts recover.
    """
    # Fast path: return existing non-empty inventory
    if market in _TDX_DYNAMIC_INVENTORY and len(_TDX_DYNAMIC_INVENTORY[market]) > 0:
        return _TDX_DYNAMIC_INVENTORY[market]

    # Slow path: initialize under lock
    with _TDX_INVENTORY_LOCK:
        # Double-check after acquiring lock
        if market in _TDX_DYNAMIC_INVENTORY and len(_TDX_DYNAMIC_INVENTORY[market]) > 0:
            return _TDX_DYNAMIC_INVENTORY[market]

        candidates = _get_dynamic_tdx_servers(market)
        snapshot = tuple(candidates)

        if len(snapshot) > 0:
            _TDX_DYNAMIC_INVENTORY[market] = snapshot

        return snapshot


def _set_tdx_status(market, path, server=None):
    """Set adapter-owned status for a market."""
    with _TDX_STATUS_LOCK:
        _TDX_STATUS[market] = {
            "path": path,
            "server": server,
            "last_success": time.time(),
        }


def _get_tdx_status(market):
    """Get adapter-owned status for a market."""
    with _TDX_STATUS_LOCK:
        return _TDX_STATUS.get(market, {}).copy()


def tdx_client(market='std'):
    """Create a validated mootdx Quotes client.

    Fallback chain:
      1) Last-good server cache (per-market)
      2) Sequential _TDX_SERVERS probe + validate (real data check) [FIXED_BUDGET]
      3) Persistent dynamic inventory (per-server error tolerance) [DYNAMIC_BUDGET]
      4) Quotes.factory(bestip=True) + validate (compatibility fallback)
      5) Bare Quotes.factory() + validate (for users with existing config)
      6) RuntimeError if all fail

    Args:
        market: 'std' for standard market, other values for extended markets

    Returns:
        mootdx.quotes.Quotes instance validated with real data fetch.

    Raises:
        RuntimeError: if all fallback stages fail.
    """
    # CRITICAL: Initialize persistent dynamic inventory BEFORE any Quotes.factory() call.
    # mootdx 0.11.7 config.setup() → bestip(sync=False) destructively pops hosts["HQ"].
    # If we snapshot after, the pool is already empty.
    dynamic_inventory = _get_or_init_dynamic_inventory(market)
    logger.info("TDX dynamic snapshot captured: %d", len(dynamic_inventory))

    # Track attempted servers to avoid duplicates within this call
    attempted_servers = set()

    def _try_once(ip, port):
        """Try a server only if not already attempted."""
        key = (ip, port)
        if key in attempted_servers:
            return None
        attempted_servers.add(key)
        return _try_server(ip, port, market)

    # Stage A: Last-good server cache (fast path)
    if market in _TDX_LAST_GOOD:
        ip, port = _TDX_LAST_GOOD[market]
        logger.info(f"TDX trying last-good server {ip}:{port}")
        client = _try_once(ip, port)
        if client is not None:
            logger.info(f"TDX server selected path=last_good server={ip}:{port}")
            _set_tdx_status(market, 'last_good', (ip, port))
            return client
        # Invalidate cache on failure
        del _TDX_LAST_GOOD[market]

    def _discover():
        """Discovery logic (runs under lock). Returns (client, server_info, path) or raises."""
        # Stage B: Fixed servers with per-stage budget
        fixed_start = time.time()
        for ip, port in _TDX_SERVERS:
            if time.time() - fixed_start > FIXED_DISCOVERY_TIME_BUDGET:
                logger.warning(f"TDX fixed stage budget exceeded ({FIXED_DISCOVERY_TIME_BUDGET}s)")
                break
            client = _try_once(ip, port)
            if client is not None:
                logger.info(f"TDX server selected path=fixed server={ip}:{port}")
                return client, (ip, port), 'fixed'

        # Stage C: Persistent dynamic inventory with independent budget
        logger.info("TDX dynamic candidates loaded: %d", len(dynamic_inventory))

        dynamic_start = time.time()
        for ip, port in dynamic_inventory:
            if time.time() - dynamic_start > DYNAMIC_DISCOVERY_TIME_BUDGET:
                logger.warning(f"TDX dynamic stage budget exceeded ({DYNAMIC_DISCOVERY_TIME_BUDGET}s)")
                break
            client = _try_once(ip, port)
            if client is not None:
                logger.info(f"TDX server selected path=dynamic server={ip}:{port}")
                return client, (ip, port), 'dynamic'

        # Stage D: bestip auto-discovery + validation (compatibility fallback)
        try:
            c = Quotes.factory(market=market, bestip=True)
            if _validate(c, market):
                logger.info("TDX server selected path=bestip")
                return c, None, 'bestip'
        except Exception:
            pass

        # Stage E: Bare factory (for users with existing ~/.mootdx/config.json)
        try:
            c = Quotes.factory(market=market)
            if _validate(c, market):
                logger.info("TDX server selected path=bare")
                return c, None, 'bare'
        except Exception:
            pass

        # Stage F: All failed
        raise RuntimeError(
            "All mootdx servers unavailable. Tried fixed servers, dynamic pool, "
            "bestip auto-discovery, and bare factory. Check network connectivity to port 7709."
        )

    # Use discovery lock to prevent concurrent scans
    with _TDX_DISCOVERY_LOCK:
        client, server_info, path = _discover()

    # Cache the successful server explicitly (we know which one succeeded)
    if server_info is not None:
        _TDX_LAST_GOOD[market] = server_info

    # Update adapter-owned status
    _set_tdx_status(market, path, server_info)

    return client


def get_prefix(code):
    """Get market prefix for a stock code.

    Args:
        code: Stock code (e.g., '600519', 'sz000001', 'sh510300')

    Returns:
        Prefix string: 'sh', 'sz', or 'bj'
    """
    code = str(code).strip()
    if code.startswith(('sh', 'sz', 'bj')):
        return code[:2]
    if code.startswith('6'):
        return 'sh'
    if code.startswith(('0', '3', '2')):
        return 'sz'
    if code.startswith('4') or code.startswith('8'):
        return 'bj'
    if code.startswith('5'):
        return 'sh'
    if code.startswith(('1', '9')):
        return 'sz'
    if code.startswith('00') and code in SH_INDEX:
        return 'sh'
    return 'sz'


def norm_ticker(code):
    """Normalize ticker to prefixed format (e.g., 'sh600519').

    Args:
        code: Stock code in any format

    Returns:
        Normalized ticker with prefix.
    """
    code = str(code).strip()
    if code.startswith(('sh', 'sz', 'bj')):
        return code.lower()
    prefix = get_prefix(code)
    return f'{prefix}{code}'
