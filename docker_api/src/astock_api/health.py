"""Health check endpoints."""
import glob
import json
import os
from fastapi import APIRouter, HTTPException

from astock_api.config import (
    UPSTREAM_VERSION, UPSTREAM_COMMIT, API_VERSION,
    BUILD_SERVICE, BUILD_IMAGE, BUILD_PHASE, BUILD_RELEASE,
    BUILD_COMMIT, BUILD_TIME
)
from astock_api.registry import list_functions

router = APIRouter()


@router.get("/health/live")
async def health_live():
    """Liveness probe - only checks if the process is alive."""
    return {"status": "ok"}


@router.get("/health/ready")
async def health_ready():
    """Readiness probe - checks modules loaded, registry populated, data/cache writable."""
    errors = []

    # Check registry has functions
    funcs = list_functions()
    if not funcs:
        errors.append("No functions registered")

    # Check data/cache dirs writable
    from astock_api.config import DATA_DIR, CACHE_DIR
    for d in [DATA_DIR, CACHE_DIR]:
        if not os.path.isdir(d):
            try:
                os.makedirs(d, exist_ok=True)
            except Exception as e:
                errors.append(f"Cannot create {d}: {e}")
        elif not os.access(d, os.W_OK):
            errors.append(f"{d} is not writable")

    if errors:
        raise HTTPException(status_code=503, detail={"errors": errors})

    return {
        "status": "ready",
        "functions_count": len(funcs),
    }


@router.get("/health/version")
async def health_version():
    """Read-only build identity metadata. No side effects."""
    return {
        "service": BUILD_SERVICE,
        "image": BUILD_IMAGE,
        "phase": BUILD_PHASE,
        "release": BUILD_RELEASE,
        "git_commit": BUILD_COMMIT,
        "api_version": API_VERSION,
        "build_time": BUILD_TIME,
    }


@router.get("/health/sources")
async def health_sources():
    """Read-only snapshot of source governor health state."""
    from astock_api.source_governor import get_governor

    governor = get_governor()
    return {"sources": governor.get_health_snapshot()}


@router.get("/health/tdx")
async def health_tdx():
    """Check mootdx connectivity with a real K-line request."""
    import time
    from astock_api.upstream.common import tdx_client, _get_tdx_status

    start = time.time()
    try:
        client = tdx_client()
        df = client.bars('600519', count=3)
        elapsed_ms = int((time.time() - start) * 1000)

        if df is None or df.empty:
            raise HTTPException(status_code=503, detail="mootdx returned empty data")

        # Read path/server from adapter-owned status (not client._server)
        status = _get_tdx_status('std')
        server_info = status.get("server")
        path = status.get("path", "unknown")
        server_str = f"{server_info[0]}:{server_info[1]}" if isinstance(server_info, tuple) and len(server_info) == 2 else "unknown"

        return {
            "ok": True,
            "source": "mootdx",
            "path": path,
            "server": server_str,
            "latency_ms": elapsed_ms,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail={
                "ok": False,
                "source": "mootdx",
                "error": str(e),
            }
        )


@router.get("/health/worker")
async def health_worker():
    """R5-C3: Read-only worker thread health status."""
    from astock_api.main import get_engine

    engine = get_engine()
    if not engine:
        raise HTTPException(status_code=503, detail={"error": "JobEngine not initialized"})

    return engine.get_worker_health()


@router.get("/health/scheduler")
async def health_scheduler():
    """Read-only scheduler state observability."""
    from astock_api.main import get_engine

    engine = get_engine()
    if not engine:
        raise HTTPException(status_code=503, detail={"error": "JobEngine not initialized"})

    from astock_api.scheduler_state import get_latest_run, initialize_scheduler_state

    # Initialize table if it doesn't exist yet (no-op if already exists)
    initialize_scheduler_state(engine)

    latest = get_latest_run(engine)
    if not latest:
        return {
            "status": "ok",
            "last_run_status": None,
            "last_run_at": None,
            "last_plan_hash": None,
            "jobs_created": 0,
        }

    return {
        "status": "ok",
        "last_run_status": latest["status"],
        "last_run_at": latest["started_at"],
        "last_plan_hash": latest.get("plan_hash"),
        "jobs_created": latest.get("created_jobs", 0),
    }


@router.get("/health/run")
async def health_run():
    """Read-only latest run lifecycle state."""
    from astock_api.main import get_engine

    engine = get_engine()
    if not engine:
        raise HTTPException(status_code=503, detail={"error": "JobEngine not initialized"})

    from astock_api.run_lifecycle import get_latest_run, initialize_run_lifecycle

    initialize_run_lifecycle(engine)

    latest = get_latest_run(engine)
    if not latest:
        return {
            "status": "ok",
            "latest_run_id": None,
            "status_field": None,
            "reference_date": None,
            "finished_at": None,
            "quality_status": None,
        }

    return {
        "status": "ok",
        "latest_run_id": latest["run_id"],
        "status_field": latest["status"],
        "reference_date": latest.get("reference_date"),
        "finished_at": latest.get("finished_at"),
        "quality_status": latest.get("quality_status"),
    }


@router.get("/health/burnin")
async def health_burnin():
    """Read-only burn-in session state.

    Reads the most recent burn-in session state file on disk (stateless —
    no in-memory session registry, safe to call at any time).
    """
    import glob

    state_files = sorted(glob.glob(os.path.join("data", "reports", "burnin", "*", "session.json")))
    if not state_files:
        return {"active": False, "session_id": None}

    with open(state_files[-1]) as f:
        state = json.load(f)

    cycles = state.get("cycles", [])
    last = cycles[-1] if cycles else None

    return {
        "active": state.get("status") == "RUNNING",
        "session_id": state.get("session_id"),
        "cycle_completed": len(cycles),
        "total_cycles": state.get("total_cycles"),
        "last_status": last.get("status") if last else None,
        "last_snapshot": (
            f"cycle_{last['cycle']:03d}.json" if last else None
        ),
    }


@router.get("/health/data")
async def health_data(reference_date: str = "2026-08-20"):
    """Read-only coverage/freshness summary for market bars data.

    Args:
        reference_date: Explicit YYYY-MM-DD date for freshness assessment.
    """
    from astock_api.dataset_store import DatasetStore
    from astock_api.coverage import get_coverage_summary

    store = DatasetStore("/app/data/astock_data.duckdb")
    store.bootstrap()

    universe_path = "/app/data/universe/instrument_universe_v2.json"
    if not os.path.exists(universe_path):
        raise HTTPException(
            status_code=503,
            detail={"error": "Universe v2 artifact not found"}
        )

    summary = get_coverage_summary(store, universe_path, reference_date)
    return {**summary, "status": "ok"}
