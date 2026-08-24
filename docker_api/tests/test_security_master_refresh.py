"""Tests for Security Master refresh pipeline (Phase 9.3-C).

Covers refresh_security_master() — the reusable external-source lifecycle:
  External Source (already acquired) → STAGING → validate
  → BSE-gated ACTIVE promotion → reject without touching current ACTIVE.

Activation policy mirrors security_master_snapshot_handler:
  - valid + BSE satisfied        → ACTIVE
  - valid, BSE missing           → VALIDATED_PARTIAL (not ACTIVE)
  - invalid                      → REJECTED (current ACTIVE untouched)

No live TDX/eastmoney: tests feed acquired row lists directly into the
in-memory DatasetStore.
"""
import pytest

from astock_api.dataset_store import DatasetStore
from astock_api import security_master as sm


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def store():
    s = DatasetStore(":memory:")
    s.bootstrap()
    return s


def _equity(exchange: str, code: str, i: int) -> dict:
    """One acquired security row in the security_master shape."""
    return {
        "code": code,
        "exchange": exchange,
        "security_type": "equity",
        "board": "main",
        "name": f"{exchange} {code} {i}",
    }


def _full_universe() -> list[dict]:
    """2000 SSE + 3000 SZSE + 250 BSE = 5250 (passes all gates + BSE)."""
    rows = []
    for i in range(2000):
        rows.append(_equity("SSE", f"6{i:05d}", i))
    for i in range(3000):
        rows.append(_equity("SZSE", f"0{i:04d}", i))
    for i in range(250):
        rows.append(_equity("BSE", f"92{i:04d}", i))
    return rows


def _no_bse_universe() -> list[dict]:
    """2000 SSE + 3000 SZSE = 5000 (passes coverage gates, no BSE)."""
    rows = []
    for i in range(2000):
        rows.append(_equity("SSE", f"6{i:05d}", i))
    for i in range(3000):
        rows.append(_equity("SZSE", f"0{i:04d}", i))
    return rows


def _bad_universe() -> list[dict]:
    """1 row — far below total coverage gate → REJECTED."""
    return [_equity("SSE", "600000", 0)]


# ── Happy path: full coverage → ACTIVE ───────────────────────────────────────

class TestRefreshHappyPath:
    def test_full_coverage_becomes_active(self, store):
        result = sm.refresh_security_master(
            store, _full_universe(), source="mootdx", as_of="2026-08-23"
        )
        assert result.status == "ACTIVE"
        assert result.row_count == 5250
        assert result.snapshot_id is not None
        assert result.checksum is not None

    def test_dataset_heads_points_to_active(self, store):
        result = sm.refresh_security_master(
            store, _full_universe(), source="mootdx", as_of="2026-08-23"
        )
        assert store.get_active_snapshot("security_master") == result.snapshot_id

    def test_exactly_one_active(self, store):
        sm.refresh_security_master(store, _full_universe(), source="mootdx", as_of="2026-08-23")
        count = store.get_conn().execute(
            "SELECT COUNT(*) FROM security_master_snapshots WHERE status='ACTIVE'"
        ).fetchone()[0]
        assert count == 1

    def test_security_id_written(self, store):
        result = sm.refresh_security_master(
            store, _full_universe(), source="mootdx", as_of="2026-08-23"
        )
        row = store.get_conn().execute(
            "SELECT security_id FROM security_master "
            "WHERE snapshot_id=? AND code='600000' AND exchange='SSE'",
            (result.snapshot_id,),
        ).fetchone()
        assert row[0] == "SSE:600000"


# ── BSE-missing → VALIDATED_PARTIAL (not ACTIVE) ─────────────────────────────

class TestRefreshPartial:
    def test_no_bse_not_active(self, store):
        result = sm.refresh_security_master(
            store, _no_bse_universe(), source="mootdx", as_of="2026-08-23"
        )
        assert result.status == "VALIDATED_PARTIAL"
        assert result.validation["valid"] is True
        assert result.validation["bse_coverage_satisfied"] is False
        # Not promoted to ACTIVE
        assert store.get_active_snapshot("security_master") is None

    def test_partial_does_not_overwrite_existing_active(self, store):
        # First: full → ACTIVE
        r1 = sm.refresh_security_master(
            store, _full_universe(), source="mootdx", as_of="2026-08-22"
        )
        assert r1.status == "ACTIVE"
        # Second: no-BSE universe (different checksum) → VALIDATED_PARTIAL
        r2 = sm.refresh_security_master(
            store, _no_bse_universe(), source="mootdx", as_of="2026-08-23"
        )
        assert r2.status == "VALIDATED_PARTIAL"
        # Current ACTIVE is still r1
        assert store.get_active_snapshot("security_master") == r1.snapshot_id


# ── Invalid → REJECTED, current ACTIVE untouched ─────────────────────────────

class TestRefreshRejected:
    def test_bad_universe_rejected(self, store):
        result = sm.refresh_security_master(
            store, _bad_universe(), source="mootdx", as_of="2026-08-23"
        )
        assert result.status == "REJECTED"
        assert store.get_active_snapshot("security_master") is None

    def test_reject_leaves_active_unchanged(self, store):
        r1 = sm.refresh_security_master(
            store, _full_universe(), source="mootdx", as_of="2026-08-22"
        )
        assert r1.status == "ACTIVE"
        r2 = sm.refresh_security_master(
            store, _bad_universe(), source="mootdx", as_of="2026-08-23"
        )
        assert r2.status == "REJECTED"
        assert store.get_active_snapshot("security_master") == r1.snapshot_id


# ── Idempotency ──────────────────────────────────────────────────────────────

class TestRefreshIdempotency:
    def test_same_checksum_source_returns_existing(self, store):
        r1 = sm.refresh_security_master(
            store, _full_universe(), source="mootdx", as_of="2026-08-22"
        )
        r2 = sm.refresh_security_master(
            store, _full_universe(), source="mootdx", as_of="2026-08-23"
        )
        assert r1.snapshot_id == r2.snapshot_id
        assert r1.checksum == r2.checksum

    def test_reimport_no_duplicate_rows(self, store):
        r1 = sm.refresh_security_master(
            store, _full_universe(), source="mootdx", as_of="2026-08-22"
        )
        conn = store.get_conn()
        before = conn.execute(
            "SELECT COUNT(*) FROM security_master WHERE snapshot_id=?",
            (r1.snapshot_id,),
        ).fetchone()[0]
        sm.refresh_security_master(store, _full_universe(), source="mootdx", as_of="2026-08-23")
        after = conn.execute(
            "SELECT COUNT(*) FROM security_master WHERE snapshot_id=?",
            (r1.snapshot_id,),
        ).fetchone()[0]
        assert before == after == 5250

    def test_different_source_bypasses_idempotency(self, store):
        r1 = sm.refresh_security_master(
            store, _full_universe(), source="mootdx", as_of="2026-08-22"
        )
        r2 = sm.refresh_security_master(
            store, _full_universe(), source="eastmoney", as_of="2026-08-23"
        )
        assert r1.snapshot_id != r2.snapshot_id


# ── Ingestion log ────────────────────────────────────────────────────────────

class TestRefreshIngestionLog:
    def test_ingestion_log_written(self, store):
        sm.refresh_security_master(store, _full_universe(), source="mootdx", as_of="2026-08-23")
        logs = store.get_conn().execute(
            "SELECT job_type, status FROM ingestion_log"
        ).fetchall()
        assert len(logs) == 1
        assert logs[0][0] == "security_master_refresh"
        assert logs[0][1] == "success"

    def test_rejected_logged_failed(self, store):
        sm.refresh_security_master(store, _bad_universe(), source="mootdx", as_of="2026-08-23")
        logs = store.get_conn().execute(
            "SELECT job_type, status FROM ingestion_log"
        ).fetchall()
        assert len(logs) == 1
        assert logs[0][0] == "security_master_refresh"
        assert logs[0][1] == "failed"


# ── Normalization ────────────────────────────────────────────────────────────

class TestRefreshNormalization:
    def test_incomplete_rows_dropped(self, store):
        rows = _full_universe()
        # Add a row missing exchange and one with empty code
        rows.append({"code": "600001", "exchange": "", "name": "no-exch"})
        rows.append({"code": "", "exchange": "SSE", "name": "no-code"})
        result = sm.refresh_security_master(
            store, rows, source="mootdx", as_of="2026-08-23"
        )
        # Incomplete rows dropped; count equals the 5250 valid rows
        assert result.row_count == 5250

    def test_empty_after_normalization_raises(self, store):
        with pytest.raises(ValueError):
            sm.refresh_security_master(
                store,
                [{"code": "", "exchange": "", "name": "junk"}],
                source="mootdx",
                as_of="2026-08-23",
            )


# ── Rollback after a refresh ─────────────────────────────────────────────────

class TestRefreshRollback:
    def test_rollback_restores_previous(self, store):
        # v1 → ACTIVE
        r1 = sm.refresh_security_master(
            store, _full_universe(), source="mootdx", as_of="2026-08-22"
        )
        # v2: one extra row → different checksum → ACTIVE (v1 → STALE)
        v2 = _full_universe() + [_equity("SSE", "699999", 999)]
        r2 = sm.refresh_security_master(
            store, v2, source="mootdx", as_of="2026-08-23"
        )
        assert r2.status == "ACTIVE"
        assert store.get_active_snapshot("security_master") == r2.snapshot_id
        # Rollback → v1
        rolled_back = store.rollback_snapshot("security_master")
        assert rolled_back == r1.snapshot_id
        assert store.get_active_snapshot("security_master") == r1.snapshot_id

    def test_rollback_exactly_one_active(self, store):
        r1 = sm.refresh_security_master(
            store, _full_universe(), source="mootdx", as_of="2026-08-22"
        )
        v2 = _full_universe() + [_equity("SSE", "699999", 999)]
        sm.refresh_security_master(store, v2, source="mootdx", as_of="2026-08-23")
        store.rollback_snapshot("security_master")
        count = store.get_conn().execute(
            "SELECT COUNT(*) FROM security_master_snapshots WHERE status='ACTIVE'"
        ).fetchone()[0]
        assert count == 1
