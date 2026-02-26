from __future__ import annotations

from typing import Any, Dict, Iterable, Set

import jwt
from fastapi import HTTPException

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("auth.internal_token")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


def _extract_scopes(payload: Dict[str, Any]) -> Set[str]:
    v = payload.get("scp")
    if isinstance(v, list):
        return {str(x).strip() for x in v if str(x).strip()}

    v = payload.get("scopes")
    if isinstance(v, list):
        return {str(x).strip() for x in v if str(x).strip()}

    v = payload.get("scope")
    if isinstance(v, str):
        return {s.strip() for s in v.split() if s.strip()}

    return set()


def verify_internal_token(
    token: str,
    *,
    secret: str,
    required_scopes: Iterable[str],
) -> Dict[str, Any]:
    if not secret:
        raise HTTPException(status_code=500, detail="Internal auth not configured")

    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    if payload.get("typ") != "internal":
        raise HTTPException(status_code=401, detail="Invalid token type")

    req = {str(x).strip() for x in required_scopes if str(x).strip()}
    scopes = _extract_scopes(payload)

    if req and not req.issubset(scopes):
        raise HTTPException(status_code=403, detail="Missing scope")

    return payload
