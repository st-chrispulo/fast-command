from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Callable
from socket_server.components.internal_auth import auth_headers

import httpx

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("socket_server.components.heartbeat")
except Exception:
    import logging

    logger = logging.getLogger(__name__)

from socket_server.core.settings import settings


async def heartbeat_loop(*, server_key: str, get_rooms_used: Callable[[], Optional[int]]) -> None:
    """Sends periodic heartbeats to the API to update server liveness."""
    url = settings.api_base_url.rstrip("/") + "/" + settings.heartbeat_endpoint_path.lstrip("/")
    timeout = httpx.Timeout(settings.api_timeout_seconds)

    while True:
        payload: Dict[str, Any] = {"server_key": server_key}

        try:
            rooms_used = get_rooms_used()
            if rooms_used is not None:
                payload["rooms_used"] = int(rooms_used)
        except Exception:
            pass

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                res = await client.post(url, json=payload, headers=await auth_headers())
            if res.status_code == 401:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    res = await client.post(url, json=payload, headers=await auth_headers(force_refresh=True))

            if res.status_code not in (200, 201, 204):
                logger.warning("[socket] heartbeat failed status=%s body=%s", res.status_code, (res.text or "")[:300])
        except Exception as e:
            logger.warning("[socket] heartbeat exception err=%s", repr(e))

        await asyncio.sleep(float(settings.heartbeat_interval_seconds or 10.0))


__all__ = ["heartbeat_loop"]