"""Update Planner: convert coverage state into a deterministic update plan.

Pure decision module — no execution, no JobEngine, no DatasetStore mutation.
Input: coverage snapshot + policy → Output: UpdatePlan JSON.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class FreshnessPolicy:
    """Explicit policy mapping freshness states to actions.

    Attributes:
        reference_date: YYYY-MM-DD date for freshness assessment.
        missing_action: Action for instruments with no data (default: BOOTSTRAP).
        stale_action: Action for instruments behind reference date (default: UPDATE).
        current_action: Action for up-to-date instruments (default: NONE).
    """

    reference_date: str
    missing_action: str = "BOOTSTRAP"
    stale_action: str = "UPDATE"
    current_action: str = "NONE"

    def action_for(self, freshness_status: str) -> str:
        """Return the configured action for a given freshness status."""
        if freshness_status == "MISSING":
            return self.missing_action
        elif freshness_status == "STALE":
            return self.stale_action
        elif freshness_status == "CURRENT":
            return self.current_action
        raise ValueError(f"Unknown freshness status: {freshness_status}")


@dataclass(frozen=True)
class UpdatePlan:
    """Immutable, deterministic update plan.

    Attributes:
        generated_at: ISO-8601 timestamp of plan generation.
        reference_date: YYYY-MM-DD date used for freshness assessment.
        universe_sha256: SHA256 of the Universe v2 artifact (provenance).
        summary: Aggregate counts by action.
        actions: Per-instrument action list (sorted by canonical_id).
    """

    generated_at: str
    reference_date: str
    universe_sha256: str | None
    summary: dict
    actions: list[dict]

    def to_dict(self) -> dict:
        """Serialize plan to a JSON-safe dict."""
        return {
            "generated_at": self.generated_at,
            "reference_date": self.reference_date,
            "universe_sha256": self.universe_sha256,
            "summary": self.summary,
            "actions": self.actions,
        }

    def to_json(self) -> str:
        """Serialize plan to a JSON string."""
        return json.dumps(self.to_dict(), indent=2)


class UpdatePlanner:
    """Convert coverage snapshot into an update plan.

    Operates on canonical identity (EXCHANGE:CODE). No execution, no side effects.
    """

    def __init__(self, policy: FreshnessPolicy):
        self.policy = policy

    def create_plan(
        self,
        coverage_snapshot: dict,
        universe_sha256: str | None = None,
    ) -> UpdatePlan:
        """Create a deterministic update plan from a coverage snapshot.

        Args:
            coverage_snapshot: Output of generate_coverage_snapshot().
                Must contain 'instruments' list and 'summary'.
            universe_sha256: Optional SHA256 of the Universe v2 artifact.

        Returns:
            Immutable UpdatePlan instance.
        """
        instruments = coverage_snapshot.get("instruments", [])

        # Build per-instrument actions (sorted by canonical_id for determinism)
        actions = []
        summary_counts = {"total": 0, "current": 0, "stale": 0, "missing": 0, "skipped": 0}

        for inst in sorted(instruments, key=lambda x: x["canonical_id"]):
            canonical_id = inst["canonical_id"]
            freshness_status = inst.get("freshness_status", "MISSING")
            latest_trade_date = inst.get("latest_trade_date")

            action = self.policy.action_for(freshness_status)
            summary_counts["total"] += 1

            # Count by freshness status
            if freshness_status == "CURRENT":
                summary_counts["current"] += 1
            elif freshness_status == "STALE":
                summary_counts["stale"] += 1
            elif freshness_status == "MISSING":
                summary_counts["missing"] += 1

            # Determine reason
            if action == "NONE":
                reason = f"Already current as of {self.policy.reference_date}"
            elif action == "BOOTSTRAP":
                reason = "No historical data available"
            elif action == "UPDATE":
                reason = f"Latest date {latest_trade_date} is behind reference {self.policy.reference_date}"
            else:
                reason = f"Policy action for {freshness_status} status"

            actions.append({
                "canonical_id": canonical_id,
                "action": action,
                "asset_type": inst.get("asset_type", "EQUITY"),
                "latest_date": latest_trade_date,
                "reason": reason,
            })

        # Count skipped (actions that are NONE)
        summary_counts["skipped"] = sum(1 for a in actions if a["action"] == "NONE")

        return UpdatePlan(
            generated_at=datetime.now(timezone.utc).isoformat(),
            reference_date=self.policy.reference_date,
            universe_sha256=universe_sha256 or coverage_snapshot.get("universe_manifest_hash"),
            summary=summary_counts,
            actions=actions,
        )

    def create_plan_from_coverage(
        self,
        store,
        universe_path: str,
    ) -> UpdatePlan:
        """Create plan by generating coverage snapshot inline.

        Args:
            store: DatasetStore instance with get_all_instrument_states().
            universe_path: Path to instrument_universe_v2.json.

        Returns:
            Immutable UpdatePlan instance.
        """
        from astock_api.coverage import generate_coverage_snapshot

        coverage = generate_coverage_snapshot(
            store, universe_path, self.policy.reference_date
        )
        return self.create_plan(coverage)


def generate_update_plan_json(
    store,
    universe_path: str,
    reference_date: str,
    output_dir: str = "/app/data/plans",
) -> dict:
    """Generate update plan and write to JSON artifact.

    Args:
        store: DatasetStore instance.
        universe_path: Path to instrument_universe_v2.json.
        reference_date: YYYY-MM-DD date for freshness assessment.
        output_dir: Directory to write plan artifact.

    Returns:
        Plan dict (same as UpdatePlan.to_dict()).
    """
    import os

    policy = FreshnessPolicy(reference_date=reference_date)
    planner = UpdatePlanner(policy)

    plan = planner.create_plan_from_coverage(store, universe_path)

    # Write artifact
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, "latest_update_plan.json")
    with open(output_path, "w") as f:
        f.write(plan.to_json())

    return plan.to_dict()
