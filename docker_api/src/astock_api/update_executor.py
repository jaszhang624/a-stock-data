"""Update Executor: convert UpdatePlan actions into JobEngine jobs.

Bridge between planning and execution — materializes approved plans as
PENDING jobs via JobEngine.create_job(). No scheduling, no execution loop.

R6-7A: Plan materialization deduplication via plan_materializations table.
Same plan_hash on second run returns existing job IDs without creating new jobs.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def compute_plan_hash(plan: dict) -> str:
    """Compute a deterministic SHA256 hash of the plan (excluding generated_at)."""
    normalized = {k: v for k, v in plan.items() if k != "generated_at"}
    return hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()[:16]


def _check_registry(engine, plan_hash: str) -> dict | None:
    """Check if a plan has already been materialized.

    Returns the registry row dict or None.
    """
    conn = engine._get_conn()
    try:
        cur = conn.execute(
            "SELECT plan_hash, created_at, job_ids, status FROM plan_materializations WHERE plan_hash=?",
            (plan_hash,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        record = dict(zip(cols, row))
        # Parse job_ids JSON
        record["job_ids"] = json.loads(record.get("job_ids", "[]"))
        return record
    except Exception:
        # Table may not exist yet (pre-R6-7A) — treat as no record
        return None
    finally:
        conn.close()


def _write_registry(engine, plan_hash: str, job_ids: list) -> None:
    """Record a successful materialization in the registry."""
    conn = engine._get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO plan_materializations (plan_hash, created_at, job_ids, status) VALUES (?, ?, ?, ?)",
            (
                plan_hash,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(job_ids),
                "materialized",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def materialize_plan(
    engine,
    plan: dict,
) -> dict:
    """Convert approved UpdatePlan actions into JobEngine jobs.

    R6-7A: Checks plan_materializations registry first. If the same
    plan_hash was already materialized, returns existing job IDs without
    creating new jobs.

    Args:
        engine: JobEngine instance with create_job().
        plan: UpdatePlan dict (from UpdatePlan.to_dict()).

    Returns:
        Materialization report with job IDs, skipped actions, etc.
    """
    plan_hash = compute_plan_hash(plan)

    # R6-7A: Check registry for existing materialization
    existing = _check_registry(engine, plan_hash)
    if existing is not None:
        return {
            "plan_hash": plan_hash,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "created_jobs": [],
            "skipped_actions": [],
            "duplicate_actions": [a["canonical_id"] for a in plan.get("actions", []) if a.get("action") in ("BOOTSTRAP", "UPDATE")],
            "existing_job_ids": existing["job_ids"],
            "already_materialized": True,
        }

    actions = plan.get("actions", [])

    # Separate by action type
    executable_actions = []
    skipped_actions = []

    for action in actions:
        if action["action"] in ("BOOTSTRAP", "UPDATE"):
            executable_actions.append(action)
        elif action["action"] == "NONE":
            skipped_actions.append(action["canonical_id"])
        else:
            logger.warning(f"Unknown action '{action['action']}' for {action['canonical_id']}, skipping")
            skipped_actions.append(action["canonical_id"])

    # Create jobs via JobEngine
    created_jobs = []
    job_ids = []

    for action in executable_actions:
        canonical_id = action["canonical_id"]
        exchange, code = canonical_id.split(":", 1) if ":" in canonical_id else ("", canonical_id)

        # Parse asset_type from plan action (if available) or default
        asset_type = action.get("asset_type", "EQUITY")

        # Determine job params based on action type
        if action["action"] == "BOOTSTRAP":
            # Bootstrap: fetch full historical data
            job_params = {
                "instruments": [{
                    "code": code,
                    "exchange": exchange,
                    "asset_type": asset_type,
                }],
                "frequency": "daily",
                "bootstrap_count": 100,
            }
        else:
            # UPDATE: incremental update from latest_date
            job_params = {
                "instruments": [{
                    "code": code,
                    "exchange": exchange,
                    "asset_type": asset_type,
                }],
                "frequency": "daily",
                "count": 10,
            }
            if action.get("latest_date"):
                job_params["target_date"] = action["latest_date"]

        # Create job via JobEngine
        try:
            job = engine.create_job("market_bars_update", job_params)
            created_jobs.append({
                "job_id": job["job_id"],
                "canonical_id": canonical_id,
                "action": action["action"],
            })
            job_ids.append(job["job_id"])
        except Exception as e:
            logger.error(f"Failed to create job for {canonical_id}: {e}")

    # R6-7A: Record successful materialization
    if job_ids:
        _write_registry(engine, plan_hash, job_ids)

    return {
        "plan_hash": plan_hash,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "created_jobs": created_jobs,
        "skipped_actions": skipped_actions,
        "duplicate_actions": [],
        "already_materialized": False,
    }


def materialize_plan_from_file(
    engine,
    plan_path: str,
) -> dict:
    """Load UpdatePlan from JSON file and materialize.

    Args:
        engine: JobEngine instance.
        plan_path: Path to latest_update_plan.json.

    Returns:
        Materialization report dict.
    """
    with open(plan_path, "r") as f:
        plan = json.load(f)

    return materialize_plan(engine, plan)


def write_materialization_report(
    report: dict,
    output_dir: str = "/app/data/plans",
) -> str:
    """Write materialization report to JSON artifact.

    Args:
        report: Materialization report dict.
        output_dir: Directory to write artifact.

    Returns:
        Path to the written report file.
    """
    output_path = Path(output_dir) / "materialization_report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)

    return str(output_path)
