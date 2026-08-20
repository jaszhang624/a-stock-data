"""R6-1: Instrument Universe Builder — targeted tests.

Tests classification, canonicalization, validation, and reproducibility
without requiring live TDX access (use fixtures instead).
"""

import json
import tempfile
from astock_api.universe_builder import (
    RawRecord,
    CanonicalRecord,
    classify_equity,
    get_index_seeds,
    canonicalize,
    validate_canonical,
    persist_canonical_artifact,
    compute_sha256,
)


# ── Fixtures ───────────────────────────────────────────────────────

def make_raw(code: str, exchange: str) -> RawRecord:
    return RawRecord(provider="mootdx", provider_market=exchange, code=code, name="")


# ── Equity Classification ─────────────────────────────────────────

class TestClassifyEquity:
    """Verify market-aware equity classification rules."""

    def test_sse_main_board(self):
        records = [make_raw("600519", "SSE")]
        classified = classify_equity(records)
        assert len(classified) == 1
        rec = classified[0]
        assert rec.canonical_id == "SSE:600519"
        assert rec.asset_type == "EQUITY"

    def test_sse_star_market(self):
        records = [make_raw("688001", "SSE")]
        classified = classify_equity(records)
        assert len(classified) == 1

    def test_szse_main_board(self):
        records = [make_raw("000001", "SZSE")]
        classified = classify_equity(records)
        assert len(classified) == 1
        rec = classified[0]
        assert rec.canonical_id == "SZSE:000001"

    def test_szse_chinext(self):
        records = [make_raw("300001", "SZSE")]
        classified = classify_equity(records)
        assert len(classified) == 1

    def test_non_equity_excluded(self):
        """Index codes should NOT be classified as equity."""
        records = [make_raw("000001", "SSE"), make_raw("399001", "SZSE")]
        classified = classify_equity(records)
        # 000001 on SSE is not equity (prefix '00' maps to SZSE)
        # 399001 on SZSE is not equity (prefix '39' not in {00, 30})
        assert len(classified) == 0

    def test_bse_unsupported(self):
        """BSE records should be skipped (no reliable source)."""
        records = [make_raw("400001", "BSE")]
        classified = classify_equity(records)
        assert len(classified) == 0

    def test_classification_source(self):
        records = [make_raw("600519", "SSE")]
        classified = classify_equity(records)
        assert classified[0].classification_source == "market_aware_prefix"


# ── Index Seeds ───────────────────────────────────────────────────

class TestIndexSeeds:
    """Verify validated index seed policy."""

    def test_seed_count(self):
        seeds = get_index_seeds()
        assert len(seeds) == 7

    def test_sse_000001_present(self):
        seeds = get_index_seeds()
        ids = [s.canonical_id for s in seeds]
        assert "SSE:000001" in ids

    def test_szse_399001_present(self):
        seeds = get_index_seeds()
        ids = [s.canonical_id for s in seeds]
        assert "SZSE:399001" in ids

    def test_classification_source(self):
        seeds = get_index_seeds()
        for seed in seeds:
            assert seed.classification_source == "validated_seed"

    def test_all_index_asset_type(self):
        seeds = get_index_seeds()
        for seed in seeds:
            assert seed.asset_type == "INDEX"


# ── Canonicalization ─────────────────────────────────────────────

class TestCanonicalize:
    """Verify deterministic sorting and deduplication."""

    def test_sort_order(self):
        records = [
            CanonicalRecord("SZSE", "000001", "SZSE:000001", "EQUITY"),
            CanonicalRecord("SSE", "000001", "SSE:000001", "INDEX"),
            CanonicalRecord("SSE", "600519", "SSE:600519", "EQUITY"),
        ]
        result = canonicalize(records)
        # Sort: asset_type → exchange → code
        assert result[0]["canonical_id"] == "SSE:600519"  # EQUITY first
        assert result[1]["canonical_id"] == "SZSE:000001"
        assert result[2]["canonical_id"] == "SSE:000001"  # INDEX after EQUITY

    def test_deduplication(self):
        records = [
            CanonicalRecord("SSE", "600519", "SSE:600519", "EQUITY"),
            CanonicalRecord("SSE", "600519", "SSE:600519", "EQUITY"),
        ]
        result = canonicalize(records)
        assert len(result) == 1

    def test_cross_market_coexistence(self):
        """SSE:000001 INDEX and SZSE:000001 EQUITY must both exist."""
        records = [
            CanonicalRecord("SSE", "000001", "SSE:000001", "INDEX"),
            CanonicalRecord("SZSE", "000001", "SZSE:000001", "EQUITY"),
        ]
        result = canonicalize(records)
        assert len(result) == 2
        ids = [r["canonical_id"] for r in result]
        assert "SSE:000001" in ids
        assert "SZSE:000001" in ids


# ── Validation ───────────────────────────────────────────────────

class TestValidateCanonical:
    """Verify invariant checks."""

    def test_valid_records(self):
        records = [
            {"exchange": "SSE", "code": "600519", "canonical_id": "SSE:600519", "asset_type": "EQUITY"},
            {"exchange": "SZSE", "code": "000001", "canonical_id": "SZSE:000001", "asset_type": "EQUITY"},
        ]
        errors = validate_canonical(records)
        assert len(errors) == 0

    def test_duplicate_canonical_id(self):
        records = [
            {"exchange": "SSE", "code": "600519", "canonical_id": "SSE:600519", "asset_type": "EQUITY"},
            {"exchange": "SSE", "code": "600519", "canonical_id": "SSE:600519", "asset_type": "EQUITY"},
        ]
        errors = validate_canonical(records)
        assert len(errors) == 1

    def test_invalid_exchange(self):
        records = [
            {"exchange": "NYSE", "code": "600519", "canonical_id": "NYSE:600519", "asset_type": "EQUITY"},
        ]
        errors = validate_canonical(records)
        assert len(errors) == 1

    def test_invalid_code(self):
        records = [
            {"exchange": "SSE", "code": "60519", "canonical_id": "SSE:60519", "asset_type": "EQUITY"},
        ]
        errors = validate_canonical(records)
        assert len(errors) == 1

    def test_canonical_id_mismatch(self):
        records = [
            {"exchange": "SSE", "code": "600519", "canonical_id": "SZSE:600519", "asset_type": "EQUITY"},
        ]
        errors = validate_canonical(records)
        assert len(errors) == 1


# ── Reproducibility ─────────────────────────────────────────────

class TestReproducibility:
    """Verify deterministic artifact generation."""

    def test_sha256_consistency(self):
        records = [
            {"exchange": "SSE", "code": "600519", "canonical_id": "SSE:600519", "asset_type": "EQUITY"},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path1 = f"{tmpdir}/art1.json"
            path2 = f"{tmpdir}/art2.json"
            _, sha1 = persist_canonical_artifact(records, path1)
            _, sha2 = persist_canonical_artifact(records, path2)
            assert sha1 == sha2

    def test_sha256_content_match(self):
        """Verify SHA256 matches actual file content."""
        records = [
            {"exchange": "SZSE", "code": "000001", "canonical_id": "SZSE:000001", "asset_type": "EQUITY"},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path, sha = persist_canonical_artifact(records, f"{tmpdir}/art.json")
            with open(path, "rb") as f:
                actual_sha = compute_sha256(f.read())
            assert sha == actual_sha
