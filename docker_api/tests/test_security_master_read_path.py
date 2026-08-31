"""Tests for Security Master read path in coverage (Phase 9.3-B).

Covers:
1. No ACTIVE snapshot → JSON fallback
2. ACTIVE snapshot → SM primary source
3. SM vs JSON equivalence (canonical_id, asset_type)
4. Asset type classification (EQUITY/INDEX/ETF)
5. ACTIVE snapshot switch → read path reflects new snapshot
6. Source label in snapshot output
"""
import json
import pytest

from astock_api.dataset_store import DatasetStore
from astock_api import security_master as sm
from astock_api.coverage import (
    _load_instruments,
    generate_coverage_snapshot,
    load_universe,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_universe(n_equity: int = 5000, n_index: int = 50, n_etf: int = 50):
    """Build a universe list with mixed asset types (total >= 4500 passes gates)."""
    instruments = []
    half_eq = n_equity // 2
    for i in range(half_eq):
        instruments.append({
            "exchange": "SSE", "code": f"6{i:05d}",
            "canonical_id": f"SSE:6{i:05d}",
            "asset_type": "EQUITY", "name": f"Eq {i}", "source": "test",
        })
    for i in range(n_equity - half_eq):
        instruments.append({
            "exchange": "SZSE", "code": f"0{i:05d}",
            "canonical_id": f"SZSE:0{i:05d}",
            "asset_type": "EQUITY", "name": f"Eq {i}", "source": "test",
        })
    half_idx = n_index // 2
    for i in range(half_idx):
        instruments.append({
            "exchange": "SSE", "code": f"9{i:05d}",
            "canonical_id": f"SSE:9{i:05d}",
            "asset_type": "INDEX", "name": f"Idx {i}", "source": "test",
        })
    for i in range(n_index - half_idx):
        instruments.append({
            "exchange": "SZSE", "code": f"3{i:05d}",
            "canonical_id": f"SZSE:3{i:05d}",
            "asset_type": "INDEX", "name": f"Idx {i}", "source": "test",
        })
    half_etf = n_etf // 2
    for i in range(half_etf):
        instruments.append({
            "exchange": "SSE", "code": f"5{i:05d}",
            "canonical_id": f"SSE:5{i:05d}",
            "asset_type": "ETF", "name": f"Etf {i}", "source": "test",
        })
    for i in range(n_etf - half_etf):
        instruments.append({
            "exchange": "SZSE", "code": f"1{i:05d}",
            "canonical_id": f"SZSE:1{i:05d}",
            "asset_type": "ETF", "name": f"Etf {i}", "source": "test",
        })
    return instruments


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def store():
    s = DatasetStore(":memory:")
    s.bootstrap()
    return s


@pytest.fixture
def mixed_universe_json(tmp_path):
    """Universe with EQUITY/INDEX/ETF, total=5100 (passes quality gates)."""
    instruments = _make_universe()
    path = str(tmp_path / "mixed.json")
    with open(path, "w") as f:
        json.dump(instruments, f)
    return path


# ── 1. JSON fallback (no ACTIVE snapshot) ────────────────────────────────────

class TestJSONFallback:
    def test_no_snapshot_returns_json(self, store, mixed_universe_json):
        inst, src = _load_instruments(store, mixed_universe_json)
        assert src == "universe_json"
        assert len(inst) == 5100

    def test_get_active_sm_returns_none(self, store):
        assert store.get_active_security_master() is None


# ── 2. SM primary source ─────────────────────────────────────────────────────

class TestSMPriority:
    def test_active_snapshot_returns_sm(self, store, mixed_universe_json):
        sm.import_universe(store, mixed_universe_json, source="test", as_of="2026-08-23")
        inst, src = _load_instruments(store, mixed_universe_json)
        assert src == "security_master"
        assert len(inst) == 5100


# ── 3. Equivalence: SM vs JSON ──────────────────────────────────────────────

class TestEquivalence:
    def test_same_canonical_ids(self, store, mixed_universe_json):
        sm.import_universe(store, mixed_universe_json, source="test", as_of="2026-08-23")
        sm_inst, _ = _load_instruments(store, mixed_universe_json)
        json_inst = load_universe(mixed_universe_json)
        assert {i["canonical_id"] for i in sm_inst} == {i["canonical_id"] for i in json_inst}

    def test_same_asset_type_per_id(self, store, mixed_universe_json):
        sm.import_universe(store, mixed_universe_json, source="test", as_of="2026-08-23")
        sm_inst, _ = _load_instruments(store, mixed_universe_json)
        json_inst = load_universe(mixed_universe_json)
        sm_map = {i["canonical_id"]: i["asset_type"] for i in sm_inst}
        json_map = {i["canonical_id"]: i["asset_type"] for i in json_inst}
        assert sm_map == json_map


# ── 4. Asset type classification ─────────────────────────────────────────────

class TestClassification:
    def test_all_types_present(self, store, mixed_universe_json):
        sm.import_universe(store, mixed_universe_json, source="test", as_of="2026-08-23")
        inst, _ = _load_instruments(store, mixed_universe_json)
        assert {"EQUITY", "INDEX", "ETF"} <= {i["asset_type"] for i in inst}

    def test_type_counts_match_json(self, store, mixed_universe_json):
        sm.import_universe(store, mixed_universe_json, source="test", as_of="2026-08-23")
        sm_inst, _ = _load_instruments(store, mixed_universe_json)
        json_inst = load_universe(mixed_universe_json)
        for t in ("EQUITY", "INDEX", "ETF"):
            sm_c = sum(1 for i in sm_inst if i["asset_type"] == t)
            json_c = sum(1 for i in json_inst if i["asset_type"] == t)
            assert sm_c == json_c, f"{t}: SM={sm_c} JSON={json_c}"


# ── 5. Snapshot switch ──────────────────────────────────────────────────────

class TestSnapshotSwitch:
    def test_read_path_follows_active_snapshot(self, store, tmp_path):
        # Universe 1: 5000 equity
        u1 = _make_universe(n_equity=5000, n_index=0, n_etf=0)
        p1 = str(tmp_path / "u1.json")
        with open(p1, "w") as f:
            json.dump(u1, f)

        # Universe 2: 5001 equity (one extra → different checksum)
        u2 = _make_universe(n_equity=5000, n_index=0, n_etf=0)
        u2.append({
            "exchange": "SSE", "code": "999999",
            "canonical_id": "SSE:999999",
            "asset_type": "EQUITY", "name": "Extra", "source": "test",
        })
        p2 = str(tmp_path / "u2.json")
        with open(p2, "w") as f:
            json.dump(u2, f)

        r1 = sm.import_universe(store, p1, source="test", as_of="2026-08-23")
        assert r1.status == "ACTIVE"
        i1, s1 = _load_instruments(store, p1)
        assert s1 == "security_master" and len(i1) == 5000

        r2 = sm.import_universe(store, p2, source="test", as_of="2026-08-24")
        assert r2.status == "ACTIVE"
        assert r2.snapshot_id != r1.snapshot_id

        i2, s2 = _load_instruments(store, p2)
        assert s2 == "security_master" and len(i2) == 5001
        assert "SSE:999999" in {i["canonical_id"] for i in i2}


# ── 6. Source label in snapshot ─────────────────────────────────────────────

class TestSourceLabel:
    def test_sm_label(self, store, mixed_universe_json, tmp_path):
        sm.import_universe(store, mixed_universe_json, source="test", as_of="2026-08-23")
        snap = generate_coverage_snapshot(
            store, mixed_universe_json, "2026-08-23", output_dir=str(tmp_path / "c1"))
        assert snap["universe_source"] == "security_master"

    def test_json_label(self, store, mixed_universe_json, tmp_path):
        snap = generate_coverage_snapshot(
            store, mixed_universe_json, "2026-08-23", output_dir=str(tmp_path / "c2"))
        assert snap["universe_source"] == "universe_json"
