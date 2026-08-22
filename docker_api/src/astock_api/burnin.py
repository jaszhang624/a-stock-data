"""Burn-In Controller: controlled production burn-in validation layer.

Answers: "Can this system operate reliably for multiple consecutive
cycles without human intervention?"

Pure orchestration — composes, does NOT modify:
  - scheduler.run_update_cycle     (one production cycle per call)
  - run_lifecycle.get_run/get_runs (ground truth)
  - operation_report.generate_daily_report (per-cycle snapshot)

No background daemon, no while-true, no cron. ``run_burn_in`` is a plain
synchronous for-loop over the explicit cycle count; each cycle is fully
persisted before the next begins.

Usage (production, manual trigger):
    session = start_burn_in(
        total_cycles=7,
        reference_dates=["2026-08-20", ..., "2026-08-26"],
        store=store, engine=engine,
        universe_path="/app/data/universe/instrument_universe_v2.json",
    )
    run_burn_in(session)
    report = finish_burn_in(session)   # burnin_final_report.json
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Session model
# ----------------------------------------------------------------------------

@dataclass
class BurnInSession:
    """A controlled multi-cycle burn-in session."""

    session_id: str
    total_cycles: int
    reference_dates: list[str]
    store: object
    engine: object
    universe_path: str
    output_dir: str
    started_at: str
    status: str = "RUNNING"
    cycles: list[dict] = field(default_factory=list)

    def state_path(self) -> str:
        return os.path.join(self.output_dir, "session.json")

    def persist(self) -> None:
        """Persist session state (data only — never live objects)."""
        state = {
            "session_id": self.session_id,
            "total_cycles": self.total_cycles,
            "reference_dates": self.reference_dates,
            "universe_path": self.universe_path,
            "output_dir": self.output_dir,
            "started_at": self.started_at,
            "status": self.status,
            "cycles": self.cycles,
        }
        os.makedirs(self.output_dir, exist_ok=True)
        with open(self.state_path(), "w") as f:
            json.dump(state, f, indent=2, default=str)

    @classmethod
    def load(cls, state_path: str, store, engine,
             universe_path: str | None = None) -> "BurnInSession":
        """Re-hydrate a session from its state file (restart recovery).

        The store/engine references are always re-injected — the persisted
        file holds only the *data*, never live objects.
        """
        with open(state_path) as f:
            state = json.load(f)
        return cls(
            session_id=state["session_id"],
            total_cycles=state["total_cycles"],
            reference_dates=state["reference_dates"],
            output_dir=state["output_dir"],
            store=store,
            engine=engine,
            universe_path=universe_path or state["universe_path"],
            started_at=state["started_at"],
            status=state["status"],
            cycles=state["cycles"],
        )


# ----------------------------------------------------------------------------
# Session lifecycle
# ----------------------------------------------------------------------------

def start_burn_in(
    total_cycles: int,
    reference_dates: list[str],
    store,
    engine,
    universe_path: str,
    output_dir: str | None = None,
) -> BurnInSession:
    """Create and persist a new burn-in session.

    Args:
        total_cycles: Number of consecutive production cycles (>= 1).
        reference_dates: One freshness reference date per cycle. Must be
            distinct — identical (universe, reference_date) pairs produce
            identical plan hashes and are blocked by plan dedup (R6-7A).
        store: DatasetStore instance.
        engine: JobEngine instance.
        universe_path: Path to instrument_universe_v2.json.
        output_dir: Where snapshots/reports land (default: data/reports/burnin).

    Returns:
        Active BurnInSession.
    """
    if total_cycles < 1:
        raise ValueError("total_cycles must be >= 1")
    if len(reference_dates) != total_cycles:
        raise ValueError(
            f"reference_dates length ({len(reference_dates)}) must equal "
            f"total_cycles ({total_cycles})"
        )
    if len(set(reference_dates)) != total_cycles:
        raise ValueError(
            "reference_dates must be distinct — identical dates produce "
            "identical plan hashes and are blocked by plan dedup (R6-7A)"
        )

    output_dir = output_dir or os.path.join("data", "reports", "burnin")
    session = BurnInSession(
        session_id=f"burnin-{uuid.uuid4().hex[:12]}",
        total_cycles=total_cycles,
        reference_dates=list(reference_dates),
        output_dir=output_dir,
        store=store,
        engine=engine,
        universe_path=universe_path,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    session.persist()
    logger.info(
        f"Burn-in session {session.session_id} started: "
        f"{total_cycles} cycles"
    )
    return session


# ----------------------------------------------------------------------------
# Cycle execution + snapshot
# ----------------------------------------------------------------------------

def _storage_snapshot(engine, store) -> dict:
    """Measure SQLite + DuckDB sizes (read-only)."""
    result: dict = {}
    try:
        db_path = getattr(engine, "db_path", None)
        if db_path and os.path.exists(db_path):
            result["sqlite_size_mb"] = round(os.path.getsize(db_path) / 1024 / 1024, 3)
        else:
            result["sqlite_size_mb"] = None
    except OSError:
        result["sqlite_size_mb"] = None
    try:
        duckdb_path = None
        if store is not None:
            duckdb_path = (
                getattr(store, "duckdb_path", None)
                or getattr(store, "_duckdb_path", None)
            )
        if duckdb_path and os.path.exists(duckdb_path):
            result["duckdb_size_mb"] = round(os.path.getsize(duckdb_path) / 1024 / 1024, 3)
        else:
            result["duckdb_size_mb"] = None
    except (OSError, AttributeError):
        result["duckdb_size_mb"] = None
    return result


def _worker_status(engine) -> dict:
    """Worker thread alive check via the existing JobEngine health API
    (read-only).

    R8-1C accuracy fix: the previous implementation checked for a
    ``worker_status`` attribute that JobEngine does not expose, so it ALWAYS
    fell through to a placeholder ``{"alive": True}`` — misreporting an
    unstarted worker as alive. Now:

      - engine exposes ``get_worker_health()`` -> real thread state:
          running  -> {"alive": true,  "state": "RUNNING"}
          stopped  -> {"alive": false, "state": "NOT_RUNNING"}
      - worker thread was never started in this engine (isolated
        orchestration harness) -> additionally "mode": "ORCHESTRATION_ONLY"
      - legacy engine without the health API -> NOT_RUNNING (never a
        placeholder True)
    """
    result = {"alive": False, "state": "NOT_RUNNING"}
    health_fn = getattr(engine, "get_worker_health", None)
    if callable(health_fn):
        try:
            status = health_fn()
        except Exception:
            status = None
        if isinstance(status, dict):
            result["alive"] = bool(
                status.get("thread_alive")
                or status.get("status") == "running"
            )
            result["state"] = "RUNNING" if result["alive"] else "NOT_RUNNING"
    if not result["alive"] and not getattr(engine, "_started", False):
        result["mode"] = "ORCHESTRATION_ONLY"
    return result


def _stable_report_sha(report: dict) -> str:
    """SHA256 of the canonical report content.

    Wall-clock fields are excluded so identical DB state + reference date
    produces identical digests across independent runs:
      - generated_at (top-level, per R6-8A)
      - scheduler.last_run_at (wall-clock timestamp of the latest run)
    """
    canonical = {k: v for k, v in report.items() if k != "generated_at"}
    if isinstance(canonical.get("scheduler"), dict):
        canonical["scheduler"] = {
            k: v for k, v in canonical["scheduler"].items()
            if k != "last_run_at"
        }
    payload = json.dumps(canonical, sort_keys=True, default=str).encode()
    return hashlib.sha256(payload).hexdigest()


def execute_burn_in_cycle(session: BurnInSession) -> dict:
    """Execute the NEXT production cycle and record its snapshot.

    One call = one full production cycle:
        run_update_cycle → lifecycle state → verification → report

    A failing cycle is recorded (status FAILED) and does NOT abort the
    session — the caller decides whether to continue.

    Returns:
        The cycle snapshot dict (also appended to session.cycles and
        persisted to cycle_NNN.json).
    """
    if len(session.cycles) >= session.total_cycles:
        raise RuntimeError("All cycles already executed")

    cycle = len(session.cycles) + 1
    reference_date = session.reference_dates[cycle - 1]
    cycle_dir = os.path.join(session.output_dir, f"cycle_{cycle:03d}")
    os.makedirs(cycle_dir, exist_ok=True)

    storage_before = _storage_snapshot(session.engine, session.store)
    t0 = time.monotonic()
    status = "SUCCESS"
    error = None
    run_id = None
    plan_hash = None
    verification: dict = {}

    try:
        from astock_api.scheduler import run_update_cycle

        result = run_update_cycle(
            session.store,
            session.engine,
            session.universe_path,
            reference_date=reference_date,
            output_dir=cycle_dir,
        )
        if isinstance(result, dict):
            run_id = result.get("run_id")
            plan_hash = result.get("plan_hash")
            verification = result.get("verification", {}) or {}
    except Exception as e:
        status = "FAILED"
        error = str(e)
        logger.error(f"Burn-in cycle {cycle} ({reference_date}) failed: {e}")

    duration_s = round(time.monotonic() - t0, 3)

    # ---- per-cycle operation report snapshot (reuse existing generator) ----
    report = None
    try:
        from astock_api.operation_report import generate_daily_report

        report = generate_daily_report(
            session.store,
            session.engine,
            session.universe_path,
            reference_date,
            output_dir=os.path.join(cycle_dir, "reports"),
        )
    except Exception as e:
        logger.warning(f"Burn-in cycle {cycle}: report generation failed: {e}")
        error = error or f"report_failed: {e}"
        status = "FAILED"

    # ---- ground truth from the lifecycle table ----------------------------
    run_record = None
    if run_id is not None:
        try:
            from astock_api.run_lifecycle import get_run

            run_record = get_run(session.engine, run_id)
        except Exception:
            pass
    if run_record is not None:
        plan_hash = plan_hash or run_record.get("plan_hash")
        verification = {
            "status": run_record.get("status"),
            "quality_status": run_record.get("quality_status"),
            "error_message": run_record.get("error_message"),
        }
        # lifecycle table is the ground truth for the cycle status
        if run_record.get("status") == "FAILED":
            status = "FAILED"
            error = error or run_record.get("error_message") or "run FAILED"

    jobs = (report or {}).get("jobs", {})
    coverage = (report or {}).get("coverage", {})
    freshness = (report or {}).get("freshness", {})
    quality_status = verification.get("quality_status")
    quality = {"quality_status": quality_status} if quality_status else {}

    storage_after = _storage_snapshot(session.engine, session.store)

    snapshot = {
        "cycle": cycle,
        "status": status,
        "run_id": run_id,
        "plan_hash": plan_hash,
        "reference_date": reference_date,
        "duration_s": duration_s,
        "jobs": {
            "created": (
                jobs.get("pending", 0) + jobs.get("running", 0)
                + jobs.get("done", 0) + jobs.get("failed", 0)
            ),
            "done": jobs.get("done", 0),
            "failed": jobs.get("failed", 0),
        },
        "coverage": coverage,
        "freshness": freshness,
        "quality": quality,
        "storage": {
            "before": storage_before,
            "after": storage_after,
        },
        "worker": _worker_status(session.engine),
        "error": error,
    }

    if report is not None:
        report_path = os.path.join(cycle_dir, "daily_operation_report.json")
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        snapshot["report_sha256"] = _stable_report_sha(report)

    snapshot_path = os.path.join(cycle_dir, f"cycle_{cycle:03d}.json")
    with open(snapshot_path, "w") as f:
        json.dump(snapshot, f, indent=2, default=str)

    session.cycles.append(snapshot)
    session.persist()
    logger.info(
        f"Burn-in cycle {cycle}/{session.total_cycles} ({reference_date}) "
        f"{status} ({duration_s}s)"
    )
    return snapshot


def run_burn_in(session: BurnInSession) -> None:
    """Execute all remaining cycles sequentially.

    Plain synchronous for-loop over the explicit cycle count — NOT a
    background daemon. Each cycle is fully persisted before the next
    begins, so an interruption at any point leaves a recoverable state
    file (see ``BurnInSession.load``).
    """
    for _ in range(session.total_cycles - len(session.cycles)):
        execute_burn_in_cycle(session)


# ----------------------------------------------------------------------------
# Final report
# ----------------------------------------------------------------------------

def _state_consistency_check(session: BurnInSession) -> dict:
    """Cross-check the session against the lifecycle table (read-only).

    Flags:
      - unfinished update_runs (statuses outside SUCCESS/FAILED)
      - orphan RUNNING jobs
      - runs whose status contradicts the recorded cycle status
    """
    from astock_api.run_lifecycle import get_runs

    issues: list[str] = []
    runs = get_runs(session.engine, limit=session.total_cycles * 2)

    unfinished = [
        r for r in runs
        if r.get("status") in ("CREATED", "RUNNING", "PLANNED",
                               "EXECUTING", "VERIFYING")
    ]
    if unfinished:
        issues.append(
            f"{len(unfinished)} unfinished update_runs: "
            + ", ".join(f"#{r['run_id']}({r['status']})" for r in unfinished)
        )

    # orphan RUNNING jobs
    try:
        conn = session.engine._get_conn()
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'RUNNING'"
            )
            orphans = cur.fetchone()[0]
            if orphans:
                issues.append(f"{orphans} orphan RUNNING jobs")
        finally:
            conn.close()
    except Exception:
        pass

    # per-cycle contradiction: cycle SUCCESS but run record FAILED
    for c in session.cycles:
        rid = c.get("run_id")
        if rid is None:
            continue
        run = next((r for r in runs if r.get("run_id") == rid), None)
        if run and c["status"] == "SUCCESS" and run.get("status") == "FAILED":
            issues.append(
                f"cycle {c['cycle']} recorded SUCCESS but run #{rid} FAILED"
            )

    return {
        "consistent": not issues,
        "issues": issues,
    }


def finish_burn_in(session: BurnInSession) -> dict:
    """Grade the session and write burnin_final_report.json.

    Grades:
      STABLE   — all cycles SUCCESS, state consistent
      DEGRADED — all cycles SUCCESS but state consistency issues found
      UNSTABLE — at least one cycle FAILED, or zero cycles executed

    Returns:
        Final report dict (also written to burnin_final_report.json).
    """
    cycles = session.cycles
    successful = [c for c in cycles if c["status"] == "SUCCESS"]
    failed = [c for c in cycles if c["status"] != "SUCCESS"]

    # quality summary across cycles
    quality_statuses = [
        c["quality"].get("quality_status") for c in cycles
        if c.get("quality", {}).get("quality_status")
    ]
    quality_summary = {
        "checked": len(quality_statuses),
        "pass": sum(1 for s in quality_statuses if s == "PASS"),
        "warn": sum(1 for s in quality_statuses if s == "WARN"),
        "fail": sum(1 for s in quality_statuses if s == "FAIL"),
    }

    # database growth (first cycle before → last cycle after)
    first = cycles[0] if cycles else {}
    last = cycles[-1] if cycles else {}
    database_growth = {
        "sqlite_size_mb": {
            "first": first.get("storage", {}).get("before", {}).get("sqlite_size_mb"),
            "last": last.get("storage", {}).get("after", {}).get("sqlite_size_mb"),
        },
        "duckdb_size_mb": {
            "first": first.get("storage", {}).get("before", {}).get("duckdb_size_mb"),
            "last": last.get("storage", {}).get("after", {}).get("duckdb_size_mb"),
        },
    }

    state_consistency = _state_consistency_check(session)

    # R8-1C: surface the real worker thread state in the final report.
    # ``state`` mirrors the per-cycle snapshot; ``mode`` is present only when
    # the worker was never started (isolated orchestration harness).
    last_worker = last.get("worker", {})
    worker_health = {
        "alive_last_cycle": last_worker.get("alive", None),
        "state_last_cycle": last_worker.get("state"),
        "mode": last_worker.get("mode"),
        "cycles_checked": len(cycles),
    }

    # idempotency: plan_hash uniqueness across cycles — repeated plans
    # (identical universe + reference date) indicate dedup interference
    plan_hashes = [c.get("plan_hash") for c in cycles if c.get("plan_hash")]
    idempotency = {
        "total_jobs_created": sum(c["jobs"]["created"] for c in cycles),
        "total_jobs_done": sum(c["jobs"]["done"] for c in cycles),
        "total_jobs_failed": sum(c["jobs"]["failed"] for c in cycles),
        "distinct_plan_hashes": len(set(plan_hashes)),
        "duplicate_plan_hashes": len(plan_hashes) != len(set(plan_hashes)),
    }

    # grade
    if not cycles:
        grade = "UNSTABLE"
    elif failed:
        grade = "UNSTABLE"
    elif state_consistency["consistent"]:
        grade = "STABLE"
    else:
        grade = "DEGRADED"

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report = {
        "session_id": session.session_id,
        "started_at": session.started_at,
        "finished_at": now,
        "total_cycles": session.total_cycles,
        "cycles_executed": len(cycles),
        "cycles_successful": len(successful),
        "cycles_failed": len(failed),
        "failed_cycles": [
            {"cycle": c["cycle"], "error": c.get("error")} for c in failed
        ],
        "worker_health": worker_health,
        "database_growth": database_growth,
        "quality_summary": quality_summary,
        "state_consistency": state_consistency,
        "idempotency": idempotency,
        "grade": grade,
    }

    os.makedirs(session.output_dir, exist_ok=True)
    path = os.path.join(session.output_dir, "burnin_final_report.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    session.status = "FINISHED"
    session.persist()
    logger.info(f"Burn-in session {session.session_id} finished: {grade}")
    return report
