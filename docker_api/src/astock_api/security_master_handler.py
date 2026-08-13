"""Security Master Handler: fetch, validate, and activate security master snapshots.

TDX acquisition (APPROVED):
- market=0 -> SZSE, market=1 -> SSE
- Uses mootdx StdQuotes.stocks(market) + equity classifier
- Raw enumeration preserved in execution artifact before filtering

Equity classifier:
- SSE: 60xxxx -> EQUITY/MAIN, 68xxxx -> EQUITY/STAR
- SZSE: 00xxxx -> EQUITY/MAIN, 30xxxx -> EQUITY/CHINEXT
- B-shares (90xxxx, 20xxxx), ETFs, bonds, repos excluded

Quality gates:
- Full CN_A snapshot requires SSE + SZSE + BSE coverage
- SSE+SZSE only -> VALIDATED (not ACTIVE) until BSE available
"""
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from astock_api.dataset_store import DatasetStore

logger = logging.getLogger(__name__)

# TDX market mapping (VERIFIED)
TDX_MARKET_SZSE = 0
TDX_MARKET_SSE = 1

# Equity code prefix classification (VERIFIED)
EQUITY_PREFIXES = {
    'SSE': {'60', '68'},   # Main board + STAR market
    'SZSE': {'00', '30'},  # Main board + ChiNext
}


def _classify_security(code: str, exchange: str) -> Optional[dict]:
    """Classify a security by code prefix. Returns equity type or None if non-equity.

    Args:
        code: 6-digit stock code
        exchange: 'SSE' or 'SZSE'

    Returns:
        dict with security_type and board, or None if non-equity
    """
    prefix = code[:2]

    if exchange == 'SSE':
        if prefix == '60':
            return {'security_type': 'equity', 'board': 'main'}
        elif prefix == '68':
            return {'security_type': 'equity', 'board': 'star'}
        # 90xxxx (B-shares), 51xxxx (ETFs), bonds, repos -> excluded
        return None

    elif exchange == 'SZSE':
        if prefix == '00':
            return {'security_type': 'equity', 'board': 'main'}
        elif prefix == '30':
            return {'security_type': 'equity', 'board': 'chinext'}
        # 20xxxx (B-shares), bonds, funds -> excluded
        return None

    # BSE or unknown exchange — pass through for now (BSE uses 92/920 family)
    return {'security_type': 'equity', 'board': 'unknown'}


def acquire_security_master_mootdx(job_id: Optional[str] = None) -> list[dict]:
    """Acquire security master from mootdx/TDX protocol.

    Uses StdQuotes.stocks(market) for market=0 (SZSE) and market=1 (SSE).
    Returns list of equity securities with code, exchange, name, etc.

    Raw enumeration is persisted as a durable artifact BEFORE classification
    at /app/data/jobs/{job_id}/raw_enumeration.json (atomic write).

    Args:
        job_id: UUID string for artifact path. If None, raw artifact is skipped.
    """
    from astock_api.upstream.common import tdx_client

    client = tdx_client()
    all_securities = []
    raw_enumeration = {}  # market_id -> list of raw rows

    # Step 1: Fetch SZSE (market=0) and SSE (market=1) — RAW FIRST
    for market_id, exchange in [(TDX_MARKET_SZSE, 'SZSE'), (TDX_MARKET_SSE, 'SSE')]:
        try:
            df = client.stocks(market_id)  # type: ignore
            logger.info(f"TDX {exchange} (market={market_id}): {len(df)} raw rows")

            # Store raw data BEFORE any classification
            raw_enumeration[exchange] = df.to_dict(orient='records')

        except Exception as e:
            logger.error(f"TDX {exchange} (market={market_id}) failed: {e}")
            raise

    # Step 2: Persist raw artifact atomically (BEFORE classification)
    if job_id:
        _persist_raw_artifact(job_id, raw_enumeration)

    # Step 3: Classify and filter equities from raw data
    for exchange, rows in raw_enumeration.items():
        for row in rows:
            code = str(row.get('code', ''))[:6]
            name = row.get('name', '')
            volunit = row.get('volunit')
            decimal_point = row.get('decimal_point')
            pre_close = row.get('pre_close')

            # Classify as equity or exclude
            classification = _classify_security(code, exchange)
            if classification is None:
                continue  # Skip non-equity (B-shares, ETFs, bonds, repos)

            all_securities.append({
                'code': code,
                'exchange': exchange,
                'name': name.strip() if name else '',
                'security_type': classification['security_type'],
                'board': classification['board'],
                'volunit': volunit,
                'decimal_point': decimal_point,
                'pre_close': pre_close,
                # list_date/delist_date: NULL (not available from TDX)
            })

    logger.info(f"TDX acquisition complete: {len(all_securities)} equities")
    return all_securities


def _persist_raw_artifact(job_id: str, raw_enumeration: dict):
    """Persist raw TDX enumeration as a durable JSON artifact.

    Uses atomic write (write to .tmp, then rename) at /app/data/jobs/{job_id}/.
    """
    import tempfile

    jobs_dir = '/app/data/jobs'
    job_dir = os.path.join(jobs_dir, str(job_id))
    os.makedirs(job_dir, exist_ok=True)

    artifact_path = os.path.join(job_dir, 'raw_enumeration.json')
    tmp_path = artifact_path + '.tmp'

    # Write to temp file first, then atomic rename
    with open(tmp_path, 'w') as f:
        json.dump(raw_enumeration, f)

    os.replace(tmp_path, artifact_path)
    logger.info(f"Raw enumeration artifact persisted: {artifact_path}")


def load_raw_artifact(job_id: str) -> dict:
    """Load raw TDX enumeration artifact for replay.

    Args:
        job_id: UUID string of the job that produced the artifact.

    Returns:
        dict with exchange -> list of raw rows
    """
    artifact_path = f'/app/data/jobs/{job_id}/raw_enumeration.json'
    with open(artifact_path, 'r') as f:
        return json.load(f)


def acquire_security_master_eastmoney() -> list[dict]:
    """Acquire security master from Eastmoney Datacenter.

    Returns list of dicts with code, exchange, name, etc.
    """
    from astock_api.upstream.eastmoney import eastmoney_datacenter

    # Try known report names that might return stock lists
    # Note: actual reportName values need testing
    securities = []

    logger.warning("Eastmoney security enumeration not yet implemented — needs upstream testing")
    return securities


def compute_checksum(securities: list[dict]) -> str:
    """Compute SHA256 checksum of sorted (exchange, code) pairs."""
    pairs = sorted([(s.get('exchange', ''), s.get('code', '')) for s in securities])
    data = '\n'.join(f"{e}:{c}" for e, c in pairs)
    return hashlib.sha256(data.encode()).hexdigest()


def security_master_snapshot_handler(payload: dict):
    """Handle security_master_snapshot job.

    Args:
        payload: {source: 'mootdx'|'eastmoney', as_of: 'YYYY-MM-DD'}

    Returns:
        dict with snapshot_id, row_count, status
    """
    from astock_api.job_engine import PermanentJobError

    source = payload.get('source', 'mootdx')
    as_of = payload.get('as_of', '')

    # 1. Fetch from upstream (pass job_id for raw artifact)
    if source == 'mootdx':
        # Get job_id from payload or context
        job_id = payload.get('job_id')
        securities = acquire_security_master_mootdx(job_id=job_id)
    elif source == 'eastmoney':
        securities = acquire_security_master_eastmoney()
    else:
        raise PermanentJobError(f"Unknown source: {source}")

    if not securities:
        raise PermanentJobError(f"No securities returned from source: {source}")

    # 2. Normalize — compute security_id
    for sec in securities:
        code = str(sec.get('code', ''))[:6]
        exchange = sec.get('exchange', '')
        if not code or not exchange:
            continue
        sec['security_id'] = f"{exchange}:{code}"

    # 3. Compute checksum
    checksum = compute_checksum(securities)

    # 4. Create STAGING snapshot
    store = DatasetStore('/app/data/astock_data.duckdb')
    snapshot_id = store.create_snapshot(source, as_of, len(securities), checksum, 'STAGING')

    # 5. Write rows
    row_count = store.write_security_master_snapshot(snapshot_id, securities)

    # 6. Validate
    validation = store.validate_snapshot(snapshot_id)

    if validation.get('valid'):
        # 7. Mark as VALIDATED (not ACTIVE yet — BSE coverage check needed)
        store.update_snapshot_status(snapshot_id, 'VALIDATED')

        # 8. Check if BSE coverage is satisfied for full CN_A activation
        if validation.get('bse_coverage_satisfied'):
            # BSE present — can activate as ACTIVE
            store.activate_snapshot('security_master', snapshot_id)
            status = 'ACTIVE'
        else:
            # BSE missing — keep as VALIDATED (not ACTIVE)
            status = 'VALIDATED_PARTIAL'
            logger.warning(f"Snapshot {snapshot_id} VALIDATED but not ACTIVE — BSE coverage missing")
    else:
        # Mark as REJECTED
        store.update_snapshot_status(snapshot_id, 'REJECTED')
        status = 'REJECTED'
        logger.error(f"Security master snapshot {snapshot_id} REJECTED: {validation.get('gates', {})}")

    return {
        "snapshot_id": snapshot_id,
        "source": source,
        "as_of": as_of,
        "row_count": row_count,
        "status": status,
        "validation": validation
    }
