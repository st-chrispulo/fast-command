from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import httpx

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("socket_server.components.internal_auth")
except Exception:
    import logging

    logger = logging.getLogger(__name__)

from socket_server.core.settings import settings


@dataclass
class _TokenState:
    access_token: str = ""
    expires_at: float = 0.0
    scopes_key: str = ""


_STATE = _TokenState()


def _token_url() -> str:
    return settings.api_base_url.rstrip("/") + "/" + settings.internal_auth_token_path.lstrip("/")


def _scopes_key() -> str:
    scopes = [str(x).strip() for x in (settings.internal_scopes or []) if str(x).strip()]
    scopes.sort()
    return ",".join(scopes)


def _is_valid() -> bool:
    if not _STATE.access_token:
        logger.debug("internal token invalid: missing token")
        return False

    if _STATE.scopes_key != _scopes_key():
        logger.debug("internal token invalid: scopes changed")
        return False

    skew = float(settings.internal_token_refresh_skew_seconds or 0)
    if time.time() >= float(_STATE.expires_at or 0) - skew:
        logger.debug("internal token invalid: expired")
        return False

    logger.debug("internal token valid: using cached token")
    return True


async def fetch_token() -> Tuple[str, float]:
    if not settings.internal_client_id or not settings.internal_client_secret:
        raise RuntimeError("Missing internal client credentials")

    timeout = httpx.Timeout(settings.api_timeout_seconds)
    scopes = [str(x).strip() for x in (settings.internal_scopes or []) if str(x).strip()]

    payload = {
        "client_id": settings.internal_client_id.strip(),
        "client_secret": settings.internal_client_secret.strip(),
        "scopes": scopes,
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        res = await client.post(_token_url(), json=payload)

    if res.status_code not in (200, 201):
        raise RuntimeError(f"internal token failed: {res.status_code} {(res.text or '')[:300]}")

    body = res.json()
    data: Dict[str, Any] = body.get("data") or body
    token = (data.get("access_token") or "").strip()
    expires_in = int(data.get("expires_in") or 300)

    if not token:
        raise RuntimeError("internal token missing access_token")

    return token, time.time() + expires_in


async def get_access_token(*, force_refresh: bool = False) -> str:
    logger.info(
        "[socket] token check cached=%s expires_at=%s scopes=%s",
        bool(_STATE.access_token),
        _STATE.expires_at,
        _STATE.scopes_key,
    )
    if not force_refresh and _is_valid():
        return _STATE.access_token

    token, expires_at = await fetch_token()
    _STATE.access_token = token
    _STATE.expires_at = float(expires_at)
    _STATE.scopes_key = _scopes_key()

    logger.info("[socket] internal token ready scopes=%s", _STATE.scopes_key)
    return _STATE.access_token


async def auth_headers(*, force_refresh: bool = False) -> Dict[str, str]:
    token = await get_access_token(force_refresh=force_refresh)
    return {"Authorization": f"Bearer {token}"}


__all__ = ["get_access_token", "auth_headers"]
