"""Health check endpoints."""
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
