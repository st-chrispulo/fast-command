from __future__ import annotations

import asyncio
import os
import socket
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

from socket_server.components.internal_auth import auth_headers
from socket_server.core.settings import settings

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("socket_server.components.registry")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


def _default_server_key() -> str:
    hn = socket.gethostname().strip() or "socket"
    pid = os.getpid()
    return f"{hn}:{pid}"


def _default_name() -> str:
    return socket.gethostname().strip() or "socket-server"


def _default_host() -> str:
    return os.getenv("HOSTNAME", "").strip() or socket.gethostname().strip() or "127.0.0.1"


def _default_port() -> Optional[int]:
    for k in ("SOCKET_SERVER_PORT", "PORT"):
        v = (os.getenv(k) or "").strip()
        if v:
            try:
                iv = int(v)
                return iv if iv > 0 else None
            except Exception:
                return None
    return None


def get_server_key() -> str:
    """Returns the server_key used for registration and heartbeat."""
    return (settings.socket_server_key or _default_server_key()).strip()


def _normalize_base_url(v: str) -> str:
    s = (v or "").strip().rstrip("/")
    if not s:
        raise ValueError("socket server host/base_url is required")

    if s.startswith("http://") or s.startswith("https://"):
        u = urlparse(s)
        if not u.hostname:
            raise ValueError("host must include a hostname")
        if not u.port:
            raise ValueError("host must include a port")
        return f"{u.scheme}://{u.hostname}:{u.port}"

    u = urlparse(f"http://{s}")
    if not u.hostname:
        raise ValueError("host must include a hostname")
    if not u.port:
        raise ValueError("host must include a port")
    return f"http://{u.hostname}:{u.port}"


def _build_base_url() -> str:
    public_url = (getattr(settings, "socket_server_public_url", "") or "").strip()
    if public_url:
        return _normalize_base_url(public_url)

    host = (getattr(settings, "socket_server_host", "") or "").strip() or _default_host()
    port = getattr(settings, "socket_server_port", None)
    if port is None:
        port = _default_port()
    if not port:
        raise ValueError("socket_server_port is required (or set SOCKET_SERVER_PORT/PORT)")

    raw = host if (host.startswith("http://") or host.startswith("https://")) else f"http://{host}"
    u = urlparse(raw)
    return f"{u.scheme}://{u.hostname}:{int(port)}"


def build_register_payload() -> Dict[str, Any]:
    """Builds the register payload for the API create endpoint."""
    server_key = get_server_key()
    base_url = _build_base_url()
    return {
        "server_key": server_key,
        "name": (settings.socket_server_name or _default_name()).strip() or None,
        "host": base_url,
        "region": (settings.socket_server_region or "").strip() or None,
        "max_rooms": int(settings.socket_server_max_rooms or 0),
        "is_active": bool(settings.socket_server_is_active),
    }


def _join_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


async def register_server() -> Dict[str, Any]:
    """Registers this socket server instance with the API."""
    url = _join_url(settings.api_base_url, settings.create_endpoint_path)
    payload = build_register_payload()
    server_key = payload["server_key"]

    timeout = httpx.Timeout(settings.api_timeout_seconds)
    limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)

    for attempt in range(1, int(settings.startup_retries or 1) + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
                headers = await auth_headers()
                res = await client.post(url, json=payload, headers=headers)

                if res.status_code == 401:
                    headers = await auth_headers(force_refresh=True)
                    res = await client.post(url, json=payload, headers=headers)

            if res.status_code in (200, 201):
                logger.info("[socket] registered server_key=%s host=%s", server_key, payload.get("host"))
                return res.json()

            if res.status_code == 409:
                logger.info("[socket] server exists server_key=%s", server_key)
                return {"status": "ok", "data": {"server_key": server_key}, "note": "already_exists"}

            logger.warning("[socket] register failed status=%s body=%s", res.status_code, (res.text or "")[:500])
        except Exception as e:
            logger.warning("[socket] register exception attempt=%s err=%s", attempt, repr(e))

        await asyncio.sleep(float(settings.startup_retry_sleep_seconds or 1.0))

    raise RuntimeError("Failed to register socket server after retries")


__all__ = ["get_server_key", "register_server", "build_register_payload"]
