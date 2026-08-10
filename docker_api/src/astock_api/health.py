"""Health check endpoints."""
import os
from fastapi import APIRouter, HTTPException

from astock_api.config import (
    UPSTREAM_VERSION, UPSTREAM_COMMIT, API_VERSION
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
