"""Instrument Identity Primitives tests.

Tests cover: parse_instrument contract, validation, ambiguity detection,
explicit conflict rejection, canonical_id derivation, collision prevention,
and DatasetStore compatibility.

Target: all tests PASS without modifying existing code.
"""
import pytest

from astock_api.instrument import (
    AmbiguousExchangeError,
    InstrumentValidationError,
    Instrument,
    parse_instrument,
)


class TestStandardEquity:
    """EQUITY context — prefix-based exchange inference."""

    def test_600519_equity(self):
        inst = parse_instrument("600519", asset_type="EQUITY")
        assert inst.exchange == "SSE"
        assert inst.code == "600519"
        assert inst.asset_type == "EQUITY"
        assert inst.canonical_id == "SSE:600519"

    def test_688001_equity(self):
        inst = parse_instrument("688001", asset_type="EQUITY")
        assert inst.canonical_id == "SSE:688001"

    def test_000001_equity(self):
        inst = parse_instrument("000001", asset_type="EQUITY")
        assert inst.canonical_id == "SZSE:000001"

    def test_300001_equity(self):
        inst = parse_instrument("300001", asset_type="EQUITY")
        assert inst.canonical_id == "SZSE:300001"


class TestGenericAmbiguity:
    """Bare code without context must raise AmbiguousExchangeError."""

    def test_000001_no_context(self):
        with pytest.raises(AmbiguousExchangeError, match="ambiguous"):
            parse_instrument("000001")

    def test_600519_no_context(self):
        """Even 6xxxxx is ambiguous without asset_type (could be INDEX)."""
        with pytest.raises(AmbiguousExchangeError, match="ambiguous"):
            parse_instrument("600519")


class TestIndexExplicitIdentity:
    """INDEX requires explicit exchange."""

    def test_000001_sse_index(self):
        inst = parse_instrument("000001", exchange="SSE", asset_type="INDEX")
        assert inst.canonical_id == "SSE:000001"

    def test_399001_szse_index(self):
        inst = parse_instrument("399001", exchange="SZSE", asset_type="INDEX")
        assert inst.canonical_id == "SZSE:399001"

    def test_index_without_exchange_raises(self):
        """INDEX without explicit exchange must fail."""
        with pytest.raises(AmbiguousExchangeError, match="exchange must be specified"):
            parse_instrument("000001", asset_type="INDEX")


class TestCollisionPrevention:
    """SSE:000001 and SZSE:000001 must be distinct."""

    def test_sse_000001_ne_szse_000001(self):
        sse = parse_instrument("000001", exchange="SSE", asset_type="INDEX")
        szse = parse_instrument("000001", asset_type="EQUITY")
        assert sse.canonical_id == "SSE:000001"
        assert szse.canonical_id == "SZSE:000001"
        assert sse.canonical_id != szse.canonical_id


class TestExplicitConflict:
    """Explicit exchange + code conflicts must be rejected."""

    def test_600519_szse_equity_conflict(self):
        """6xxxxx EQUITY belongs to SSE, not SZSE."""
        with pytest.raises(InstrumentValidationError, match="belongs to SSE"):
            parse_instrument("600519", exchange="SZSE", asset_type="EQUITY")

    def test_000001_sse_equity_conflict(self):
        """0xxxxx EQUITY belongs to SZSE, not SSE."""
        with pytest.raises(InstrumentValidationError, match="belongs to SZSE"):
            parse_instrument("000001", exchange="SSE", asset_type="EQUITY")

    def test_300001_sse_equity_conflict(self):
        """3xxxxx EQUITY belongs to SZSE, not SSE."""
        with pytest.raises(InstrumentValidationError, match="belongs to SZSE"):
            parse_instrument("300001", exchange="SSE", asset_type="EQUITY")


class TestInvalidInputs:
    """Validation must reject malformed inputs."""

    def test_empty_code(self):
        with pytest.raises(InstrumentValidationError, match="6 digits"):
            parse_instrument("")

    def test_short_code(self):
        with pytest.raises(InstrumentValidationError, match="6 digits"):
            parse_instrument("60051")

    def test_long_code(self):
        with pytest.raises(InstrumentValidationError, match="6 digits"):
            parse_instrument("6005190")

    def test_non_numeric_code(self):
        with pytest.raises(InstrumentValidationError, match="6 digits"):
            parse_instrument("ABC001")

    def test_none_code(self):
        with pytest.raises(InstrumentValidationError, match="non-empty"):
            parse_instrument(None)  # type: ignore

    def test_invalid_exchange(self):
        with pytest.raises(InstrumentValidationError, match="exchange must be"):
            parse_instrument("600519", exchange="NYSE")

    def test_invalid_asset_type(self):
        with pytest.raises(InstrumentValidationError, match="asset_type must be"):
            parse_instrument("600519", asset_type="ETF")

    def test_case_insensitive_exchange(self):
        """Exchange should accept lowercase and normalize."""
        inst = parse_instrument("600519", exchange="sse", asset_type="EQUITY")
        assert inst.exchange == "SSE"

    def test_case_insensitive_asset_type(self):
        """Asset type should accept lowercase and normalize."""
        inst = parse_instrument("600519", asset_type="equity")
        assert inst.asset_type == "EQUITY"


class TestCanonicalIdDerivation:
    """canonical_id must be derived from exchange + code."""

    def test_canonical_id_format(self):
        inst = parse_instrument("600519", asset_type="EQUITY")
        assert inst.canonical_id == f"{inst.exchange}:{inst.code}"

    def test_canonical_id_not_mutable(self):
        """Instrument is frozen — fields cannot be changed."""
        inst = parse_instrument("600519", asset_type="EQUITY")
        with pytest.raises(Exception):  # FrozenInstanceError / dataclass frozen
            inst.exchange = "FAKE"  # type: ignore

    def test_str_returns_canonical_id(self):
        inst = parse_instrument("600519", asset_type="EQUITY")
        assert str(inst) == "SSE:600519"


class TestDatasetStoreCompatibility:
    """New primitives must align with DatasetStore naming."""

    def test_sse_600519_matches_dataset_store(self):
        """DatasetStore.make_security_id('600519') returns 'SSE:600519'."""
        from astock_api.dataset_store import DatasetStore

        inst = parse_instrument("600519", asset_type="EQUITY")
        ds_id = DatasetStore.make_security_id("600519")
        assert inst.canonical_id == ds_id

    def test_szse_000001_matches_dataset_store(self):
        from astock_api.dataset_store import DatasetStore

        inst = parse_instrument("000001", asset_type="EQUITY")
        ds_id = DatasetStore.make_security_id("000001")
        assert inst.canonical_id == ds_id

    def test_code_to_exchange_consistency(self):
        """code_to_exchange mapping must match equity prefix mapping."""
        from astock_api.dataset_store import DatasetStore

        # SSE codes
        assert DatasetStore.code_to_exchange("600519") == "SSE"
        assert DatasetStore.code_to_exchange("688001") == "SSE"
        # SZSE codes
        assert DatasetStore.code_to_exchange("000001") == "SZSE"
        assert DatasetStore.code_to_exchange("300001") == "SZSE"
