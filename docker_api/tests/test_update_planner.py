"""R6-5 Update Planner targeted tests.

Covers:
A. Missing instrument produces BOOTSTRAP
B. Current instrument produces NONE
C. Stale instrument produces UPDATE
D. SSE:000001 INDEX isolation
E. SZSE:000001 EQUITY isolation
F. Deterministic output: same input => identical plan
G. Universe hash preserved
"""

import pytest

from astock_api.update_planner import (
    FreshnessPolicy,
    UpdatePlan,
    UpdatePlanner,
)


# A. Missing instrument produces BOOTSTRAP
def test_missing_instrument_produces_bootstrap():
    policy = FreshnessPolicy(reference_date="2026-08-20")
    planner = UpdatePlanner(policy)

    coverage = {
        "instruments": [
            {
                "canonical_id": "SSE:600519",
                "freshness_status": "MISSING",
                "latest_trade_date": None,
            }
        ]
    }

    plan = planner.create_plan(coverage)
    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action["canonical_id"] == "SSE:600519"
    assert action["action"] == "BOOTSTRAP"


# B. Current instrument produces NONE
def test_current_instrument_produces_none():
    policy = FreshnessPolicy(reference_date="2026-08-20")
    planner = UpdatePlanner(policy)

    coverage = {
        "instruments": [
            {
                "canonical_id": "SSE:600519",
                "freshness_status": "CURRENT",
                "latest_trade_date": "2026-08-20",
            }
        ]
    }

    plan = planner.create_plan(coverage)
    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action["canonical_id"] == "SSE:600519"
    assert action["action"] == "NONE"


# C. Stale instrument produces UPDATE
def test_stale_instrument_produces_update():
    policy = FreshnessPolicy(reference_date="2026-08-20")
    planner = UpdatePlanner(policy)

    coverage = {
        "instruments": [
            {
                "canonical_id": "SSE:600519",
                "freshness_status": "STALE",
                "latest_trade_date": "2026-08-18",
            }
        ]
    }

    plan = planner.create_plan(coverage)
    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action["canonical_id"] == "SSE:600519"
    assert action["action"] == "UPDATE"


# D. SSE:000001 INDEX isolation
def test_sse_000001_index_isolation():
    policy = FreshnessPolicy(reference_date="2026-08-20")
    planner = UpdatePlanner(policy)

    coverage = {
        "instruments": [
            {
                "canonical_id": "SSE:000001",
                "asset_type": "INDEX",
                "freshness_status": "CURRENT",
                "latest_trade_date": "2026-08-20",
            },
            {
                "canonical_id": "SZSE:000001",
                "asset_type": "EQUITY",
                "freshness_status": "STALE",
                "latest_trade_date": "2026-08-18",
            },
        ]
    }

    plan = planner.create_plan(coverage)
    actions_by_id = {a["canonical_id"]: a for a in plan.actions}

    # SSE:000001 INDEX should be NONE (current)
    sse_action = actions_by_id["SSE:000001"]
    assert sse_action["action"] == "NONE"

    # SZSE:000001 EQUITY should be UPDATE (stale)
    szse_action = actions_by_id["SZSE:000001"]
    assert szse_action["action"] == "UPDATE"


# E. SZSE:000001 EQUITY isolation
def test_szse_000001_equity_isolation():
    policy = FreshnessPolicy(reference_date="2026-08-20")
    planner = UpdatePlanner(policy)

    coverage = {
        "instruments": [
            {
                "canonical_id": "SZSE:000001",
                "asset_type": "EQUITY",
                "freshness_status": "MISSING",
                "latest_trade_date": None,
            },
        ]
    }

    plan = planner.create_plan(coverage)
    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action["canonical_id"] == "SZSE:000001"
    assert action["action"] == "BOOTSTRAP"


# F. Deterministic output: same input => identical plan
def test_deterministic_output():
    policy = FreshnessPolicy(reference_date="2026-08-20")
    planner = UpdatePlanner(policy)

    coverage = {
        "instruments": [
            {"canonical_id": "SSE:600519", "freshness_status": "STALE", "latest_trade_date": "2026-08-18"},
            {"canonical_id": "SZSE:000001", "freshness_status": "MISSING", "latest_trade_date": None},
            {"canonical_id": "SSE:000001", "freshness_status": "CURRENT", "latest_trade_date": "2026-08-20"},
        ]
    }

    # Generate plan twice (generated_at will differ, but actions should be identical)
    plan1 = planner.create_plan(coverage)
    plan2 = planner.create_plan(coverage)

    # Actions must be identical (sorted by canonical_id)
    assert plan1.actions == plan2.actions

    # Summary must be identical
    assert plan1.summary == plan2.summary


# G. Universe hash preserved
def test_universe_hash_preserved():
    policy = FreshnessPolicy(reference_date="2026-08-20")
    planner = UpdatePlanner(policy)

    coverage = {
        "universe_manifest_hash": "f1011f3888f4e930d9e88de2d5be8083b7cb63ea528eb234add4eb290ebca403",
        "instruments": [
            {"canonical_id": "SSE:600519", "freshness_status": "STALE", "latest_trade_date": "2026-08-18"},
        ]
    }

    plan = planner.create_plan(coverage)
    assert plan.universe_sha256 == "f1011f3888f4e930d9e88de2d5be8083b7cb63ea528eb234add4eb290ebca403"


def test_empty_universe_produces_valid_plan():
    policy = FreshnessPolicy(reference_date="2026-08-20")
    planner = UpdatePlanner(policy)

    coverage = {"instruments": []}

    plan = planner.create_plan(coverage)
    assert len(plan.actions) == 0
    assert plan.summary["total"] == 0


def test_policy_custom_actions():
    """Test that custom policy actions are respected."""
    policy = FreshnessPolicy(
        reference_date="2026-08-20",
        missing_action="SKIP",
        stale_action="UPDATE",
        current_action="NONE",
    )
    planner = UpdatePlanner(policy)

    coverage = {
        "instruments": [
            {"canonical_id": "SSE:600519", "freshness_status": "MISSING", "latest_trade_date": None},
        ]
    }

    plan = planner.create_plan(coverage)
    assert plan.actions[0]["action"] == "SKIP"


def test_plan_serialization():
    """Test that plan serializes to JSON correctly."""
    policy = FreshnessPolicy(reference_date="2026-08-20")
    planner = UpdatePlanner(policy)

    coverage = {
        "instruments": [
            {"canonical_id": "SSE:600519", "freshness_status": "STALE", "latest_trade_date": "2026-08-18"},
        ]
    }

    plan = planner.create_plan(coverage)
    d = plan.to_dict()
    assert "generated_at" in d
    assert d["reference_date"] == "2026-08-20"
    assert len(d["actions"]) == 1

    # Verify JSON round-trip
    json_str = plan.to_json()
    import json

    parsed = json.loads(json_str)
    assert parsed["reference_date"] == "2026-08-20"
    assert len(parsed["actions"]) == 1


def test_summary_counts_correct():
    """Test that summary counts match the actions."""
    policy = FreshnessPolicy(reference_date="2026-08-20")
    planner = UpdatePlanner(policy)

    coverage = {
        "instruments": [
            {"canonical_id": "SSE:600519", "freshness_status": "STALE", "latest_trade_date": "2026-08-18"},
            {"canonical_id": "SZSE:000001", "freshness_status": "MISSING", "latest_trade_date": None},
            {"canonical_id": "SSE:000001", "freshness_status": "CURRENT", "latest_trade_date": "2026-08-20"},
        ]
    }

    plan = planner.create_plan(coverage)
    assert plan.summary["total"] == 3
    assert plan.summary["stale"] == 1
    assert plan.summary["missing"] == 1
    assert plan.summary["current"] == 1
    assert plan.summary["skipped"] == 1  # NONE action count


def test_actions_sorted_by_canonical_id():
    """Test that actions are sorted by canonical_id for determinism."""
    policy = FreshnessPolicy(reference_date="2026-08-20")
    planner = UpdatePlanner(policy)

    coverage = {
        "instruments": [
            {"canonical_id": "SZSE:000001", "freshness_status": "STALE", "latest_trade_date": "2026-08-18"},
            {"canonical_id": "SSE:600519", "freshness_status": "STALE", "latest_trade_date": "2026-08-18"},
            {"canonical_id": "SSE:000001", "freshness_status": "CURRENT", "latest_trade_date": "2026-08-20"},
        ]
    }

    plan = planner.create_plan(coverage)
    canonical_ids = [a["canonical_id"] for a in plan.actions]
    assert canonical_ids == sorted(canonical_ids)
