"""API security - lightweight API key authentication."""
import hmac
import os
from fastapi import Header, HTTPException, Depends
from typing import Optional

from astock_api.config import ASTOCK_API_KEY


def verify_api_key(x_api_key: Optional[str] = Header(None)):
    """Verify X-API-Key header. Fail-fast if ASTOCK_API_KEY not configured."""
    if not ASTOCK_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="ASTOCK_API_KEY is required but not configured"
        )

    if x_api_key is None:
        raise HTTPException(
            status_code=401,
            detail="X-API-Key header is required"
        )

    if not hmac.compare_digest(x_api_key, ASTOCK_API_KEY):
        raise HTTPException(
            status_code=401,
            detail="Invalid API key"
        )
