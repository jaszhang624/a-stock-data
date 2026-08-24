"""Security Master lifecycle orchestration (Phase 9.3-A).

Pure orchestration layer — composes existing DatasetStore primitives:
  - create_snapshot()       → STAGING
  - write_security_master_snapshot() → rows
  - validate_snapshot()     → quality gates
  - update_snapshot_status() → VALIDATED / REJECTED
  - activate_snapshot()     → ACTIVE (atomic pointer switch)
  - rollback_snapshot()     → restore previous ACTIVE

Does NOT modify DatasetStore internals or re-implement DB logic.

Scope (P9.3-A only):
  - Universe JSON import → snapshot lifecycle
  - Quality gate thresholds (centralized)
  - Rollback (restore previous ACTIVE)
  - Rejection (explicit + gate-driven)
  - Idempotency (same checksum + source → existing snapshot)

NOT in scope:
  - TDX/eastmoney upstream fetch (security_master_handler.py)
  - Production read path switch (P9.3-B)
  - Daily automation / cron
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from astock_api.dataset_store import DatasetStore

logger = logging.getLogger(__name__)


# ── Quality Gate Thresholds (centralized) ────────────────────────────────────

@dataclass(frozen=True)
class GateThresholds:
    """Centralized quality gate thresholds for security master validation.

    These mirror the logic in DatasetStore.validate_snapshot().
    Changing them here documents intent; the actual enforcement lives in
    validate_snapshot() which reads its own inline values. To keep a single
    source of truth, validate_snapshot() is the enforcement point; this
    dataclass serves as documentation and for test assertions.
    """
    min_total: int = 4500
    min_sse: int = 1800
    min_szse: int = 2500
    min_bse: int = 200
    max_delta_pct: float = 0.3


THRESHOLDS = GateThresholds()


# ── Result models ────────────────────────────────────────────────────────────

@dataclass
class ImportResult:
    """Result of a universe import operation."""
    snapshot_id: str
    status: str          # STAGING / VALIDATED / ACTIVE / REJECTED
    row_count: int
    checksum: str
    validation: Optional[dict] = None


# ── Checksum ─────────────────────────────────────────────────────────────────

def compute_universe_checksum(instruments: list[dict]) -> str:
    """Compute stable SHA256 checksum from sorted (exchange, code) pairs.

    Order-independent: same instruments in any order produce the same hash.

    Args:
        instruments: List of dicts with at least 'exchange' and 'code'.

    Returns:
        Hex SHA256 string.
    """
    pairs = sorted(
        (str(inst.get("exchange", "")), str(inst.get("code", "")))
        for inst in instruments
    )
    data = "\n".join(f"{e}:{c}" for e, c in pairs)
    return hashlib.sha256(data.encode()).hexdigest()


# ── Import ───────────────────────────────────────────────────────────────────

def import_universe(
    store: DatasetStore,
    json_path: str,
    source: str = "universe_json",
    as_of: str | None = None,
) -> ImportResult:
    """Import instrument_universe_v2.json into a security master snapshot.

    Lifecycle:
      1. Read JSON (list of instruments)
      2. Compute checksum (sorted exchange:code pairs)
      3. Idempotency check: same checksum + source → return existing
      4. Create STAGING snapshot
      5. Write rows
      6. Validate (quality gates)
      7. Transition: VALIDATED → attempt activate → ACTIVE
                      or REJECTED (gates failed)

    Args:
        store: DatasetStore instance (must be bootstrapped).
        json_path: Path to instrument_universe_v2.json.
        source: Provenance label (default: 'universe_json').
        as_of: Snapshot date YYYY-MM-DD. Defaults to today (UTC).

    Returns:
        ImportResult with snapshot_id, status, row_count, checksum.

    Raises:
        FileNotFoundError: json_path does not exist.
        ValueError: JSON is not a list of instruments.
    """
    if as_of is None:
        from datetime import datetime, timezone
        as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 1. Read
    if not os.path.exists(json_path):
        raise FileNotFoundError(f"Universe JSON not found: {json_path}")

    with open(json_path) as f:
        instruments = json.load(f)

    if not isinstance(instruments, list):
        raise ValueError(
            f"Expected JSON array of instruments, got {type(instruments).__name__}"
        )
    if not instruments:
        raise ValueError("Universe JSON is empty")

    # 2. Checksum
    checksum = compute_universe_checksum(instruments)

    # 3. Idempotency check: look for existing snapshot with same checksum
    conn = store.get_conn()
    existing = conn.execute(
        "SELECT snapshot_id, status FROM security_master_snapshots "
        "WHERE checksum=? AND source=?",
        (checksum, source),
    ).fetchone()

    if existing:
        existing_id, existing_status = existing[0], existing[1]
        row_count = conn.execute(
            "SELECT COUNT(*) FROM security_master WHERE snapshot_id=?",
            (existing_id,),
        ).fetchone()[0]
        logger.info(
            f"Import idempotent: snapshot {existing_id} already exists "
            f"(status={existing_status}, rows={row_count})"
        )
        return ImportResult(
            snapshot_id=existing_id,
            status=existing_status,
            row_count=row_count,
            checksum=checksum,
        )

    # 4. Create STAGING snapshot
    row_count = len(instruments)
    snapshot_id = store.create_snapshot(
        source=source,
        as_of=as_of,
        row_count=row_count,
        checksum=checksum,
        status="STAGING",
    )

    # 5. Write rows
    securities = []
    for inst in instruments:
        exchange = inst.get("exchange", "")
        code = str(inst.get("code", ""))[:6]
        securities.append({
            "code": code,
            "exchange": exchange,
            "security_type": inst.get("asset_type", "EQUITY").lower(),
            "board": None,  # not present in universe JSON
            "name": inst.get("name", ""),
            "listing_status": "active",
            "list_date": None,
            "delist_date": None,
            "source": inst.get("source", source),
            "as_of": as_of,
        })

    written = store.write_security_master_snapshot(snapshot_id, securities)

    # 6. Validate
    validation = store.validate_snapshot(snapshot_id)

    # 7. Transition
    if validation.get("valid"):
        store.update_snapshot_status(snapshot_id, "VALIDATED")
        # Attempt activation
        try:
            store.activate_snapshot("security_master", snapshot_id)
            status = "ACTIVE"
        except ValueError as e:
            status = "VALIDATED"
            logger.warning(f"Activation failed (non-blocking): {e}")
    else:
        store.update_snapshot_status(snapshot_id, "REJECTED")
        status = "REJECTED"
        logger.error(
            f"Snapshot {snapshot_id} REJECTED: "
            f"gates={validation.get('gates', {})}"
        )

    # Log ingestion
    try:
        store.log_ingestion(
            job_type="security_master_snapshot",
            job_id=snapshot_id,
            table_name="security_master",
            rows_inserted=written,
            rows_updated=0,
            rows_unchanged=0,
            status="success" if status != "REJECTED" else "failed",
        )
    except Exception as e:
        logger.warning(f"Ingestion log failed (non-blocking): {e}")

    return ImportResult(
        snapshot_id=snapshot_id,
        status=status,
        row_count=written,
        checksum=checksum,
        validation=validation,
    )


# ── Rollback ─────────────────────────────────────────────────────────────────

def rollback_active(store: DatasetStore, dataset_name: str = "security_master") -> Optional[str]:
    """Rollback the active snapshot pointer to the previous one.

    Delegates to DatasetStore.rollback_snapshot() which handles:
      - Finding the previous VALIDATED/ACTIVE snapshot
      - Atomically updating dataset_heads pointer
      - Setting old ACTIVE → STALE, new ACTIVE → ACTIVE

    Returns:
        The snapshot_id now pointing as ACTIVE, or None if no
        rollback is possible (only one snapshot exists).
    """
    return store.rollback_snapshot(dataset_name)


# ── Rejection ────────────────────────────────────────────────────────────────

def reject_snapshot(store: DatasetStore, snapshot_id: str, reason: str = "") -> None:
    """Explicitly reject a STAGING or VALIDATED snapshot.

    Args:
        store: DatasetStore instance.
        snapshot_id: UUID of the snapshot to reject.
        reason: Human-readable rejection reason (logged only).
    """
    store.update_snapshot_status(snapshot_id, "REJECTED")
    logger.info(f"Snapshot {snapshot_id} explicitly rejected: {reason}")


# ── Refresh Pipeline (P9.3-C) ────────────────────────────────────────────────

def refresh_security_master(
    store: DatasetStore,
    securities: list[dict],
    source: str,
    as_of: str | None = None,
) -> ImportResult:
    """Security Master refresh pipeline (Phase 9.3-C).

    Reusable lifecycle primitive for an external source:
      External Source → latest universe (already acquired)
        → STAGING snapshot → validate → ACTIVE promotion
        → reject invalid snapshot without affecting current ACTIVE.

    Consumes an ALREADY-ACQUIRED list of securities, decoupled from the
    live TDX/eastmoney fetch in security_master_handler.py. The activation
    policy mirrors security_master_snapshot_handler exactly:
      - valid + BSE coverage satisfied → ACTIVE
      - valid, BSE missing            → VALIDATED (reported as VALIDATED_PARTIAL)
      - invalid                       → REJECTED (current ACTIVE untouched)

    Lifecycle:
      1. Normalize rows (6-digit code + security_id; drop incomplete rows)
      2. Compute deterministic checksum (sorted exchange:code pairs)
      3. Idempotency: same checksum + source → return existing snapshot
      4. Create STAGING snapshot
      5. Write rows
      6. Validate (quality gates)
      7. BSE-gated promotion
      8. Record ingestion (non-blocking)

    Args:
        store: DatasetStore instance (must be bootstrapped).
        securities: List of dicts with at least 'code' and 'exchange'.
        source: Provenance label (e.g. 'mootdx').
        as_of: Snapshot date YYYY-MM-DD. Defaults to today (UTC).

    Returns:
        ImportResult with snapshot_id, status, row_count, checksum, validation.

    Raises:
        ValueError: no valid securities remain after normalization.
    """
    if as_of is None:
        from datetime import datetime, timezone
        as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 1. Normalize (truncate code, set security_id, drop incomplete rows)
    normalized = []
    for sec in securities:
        code = str(sec.get("code", ""))[:6]
        exchange = str(sec.get("exchange", ""))
        if not code or not exchange:
            logger.warning("refresh_security_master: skipping incomplete row %r", sec)
            continue
        sec["code"] = code
        sec["security_id"] = f"{exchange}:{code}"
        normalized.append(sec)

    if not normalized:
        raise ValueError(
            "refresh_security_master: no valid securities after normalization"
        )

    # 2. Checksum
    checksum = compute_universe_checksum(normalized)

    # 3. Idempotency: look for existing snapshot with same checksum
    conn = store.get_conn()
    existing = conn.execute(
        "SELECT snapshot_id, status FROM security_master_snapshots "
        "WHERE checksum=? AND source=?",
        (checksum, source),
    ).fetchone()

    if existing:
        existing_id, existing_status = existing[0], existing[1]
        row_count = conn.execute(
            "SELECT COUNT(*) FROM security_master WHERE snapshot_id=?",
            (existing_id,),
        ).fetchone()[0]
        logger.info(
            f"Refresh idempotent: snapshot {existing_id} already exists "
            f"(status={existing_status}, rows={row_count})"
        )
        return ImportResult(
            snapshot_id=existing_id,
            status=existing_status,
            row_count=row_count,
            checksum=checksum,
        )

    # 4. Create STAGING snapshot
    snapshot_id = store.create_snapshot(
        source=source,
        as_of=as_of,
        row_count=len(normalized),
        checksum=checksum,
        status="STAGING",
    )

    # 5. Write rows
    written = store.write_security_master_snapshot(snapshot_id, normalized)

    # 6. Validate
    validation = store.validate_snapshot(snapshot_id)

    # 7. BSE-gated promotion (mirrors security_master_snapshot_handler)
    if validation.get("valid"):
        store.update_snapshot_status(snapshot_id, "VALIDATED")
        if validation.get("bse_coverage_satisfied"):
            store.activate_snapshot("security_master", snapshot_id)
            status = "ACTIVE"
        else:
            status = "VALIDATED_PARTIAL"
            logger.warning(
                f"Snapshot {snapshot_id} VALIDATED but not ACTIVE — BSE coverage missing"
            )
    else:
        store.update_snapshot_status(snapshot_id, "REJECTED")
        status = "REJECTED"
        logger.error(
            f"Security master refresh snapshot {snapshot_id} REJECTED: "
            f"gates={validation.get('gates', {})}"
        )

    # 8. Log ingestion (non-blocking)
    try:
        store.log_ingestion(
            job_type="security_master_refresh",
            job_id=snapshot_id,
            table_name="security_master",
            rows_inserted=written,
            rows_updated=0,
            rows_unchanged=0,
            status="success" if status != "REJECTED" else "failed",
        )
    except Exception as e:
        logger.warning(f"Ingestion log failed (non-blocking): {e}")

    return ImportResult(
        snapshot_id=snapshot_id,
        status=status,
        row_count=written,
        checksum=checksum,
        validation=validation,
    )
