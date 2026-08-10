"""Job management API routes."""
from fastapi import APIRouter, HTTPException, Depends, Query, Request

from astock_api.security import verify_api_key
from astock_api.job_engine import JobEngine, JOB_TYPES

router = APIRouter(
    prefix="/api/v1/jobs",
    tags=["jobs"],
    dependencies=[Depends(verify_api_key)]
)


def get_engine(request: Request):
    """Get the job engine instance from app state."""
    engine = getattr(request.app.state, "job_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="Job engine not available")
    return engine


@router.post("")
async def create_job(body: dict, request: Request):
    """Create a new job."""
    engine = get_engine(request)

    job_type = body.get("job_type", "")
    params = body.get("params", {})

    if not job_type or job_type not in JOB_TYPES:
        raise HTTPException(status_code=422, detail=f"Unknown job type: {job_type}")

    try:
        result = engine.create_job(job_type, params)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return result


@router.get("/{job_id}")
async def get_job(job_id: str, request: Request):
    """Get job status."""
    engine = get_engine(request)

    job = engine.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return job


@router.get("/{job_id}/chunks")
async def get_chunks(
    job_id: str,
    request: Request,
    status: str = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500)
):
    """Get chunks for a job."""
    engine = get_engine(request)

    job = engine.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    chunks = engine.get_chunks(job_id, status=status, limit=limit)
    return {"job_id": job_id, "chunks": chunks}


@router.get("")
async def list_jobs(
    request: Request,
    status: str = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200)
):
    """List jobs."""
    engine = get_engine(request)

    jobs = engine.list_jobs(status=status, limit=limit)
    return {"jobs": jobs}


@router.post("/{job_id}/pause")
async def pause_job(job_id: str, request: Request):
    """Pause a job."""
    engine = get_engine(request)

    try:
        engine.pause_job(job_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"job_id": job_id, "status": "PAUSED"}


@router.post("/{job_id}/resume")
async def resume_job(job_id: str, request: Request):
    """Resume a paused job."""
    engine = get_engine(request)

    try:
        engine.resume_job(job_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"job_id": job_id, "status": "PENDING"}


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request):
    """Cancel a job."""
    engine = get_engine(request)

    try:
        engine.cancel_job(job_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"job_id": job_id, "status": "CANCELLED"}
