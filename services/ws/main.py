# socket_server/main.py

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Dict

import socketio
from fastapi import FastAPI

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("socket_server.main")
except Exception:
    import logging

    logger = logging.getLogger(__name__)

from socket_server.components.heartbeat import heartbeat_loop
from socket_server.components.internal_auth import get_access_token
from socket_server.components.registry import get_server_key, register_server
from socket_server.core.settings import settings
from socket_server.sockets.events import register_socket_events


def _normalize_cors_allowed_origins(value: Any) -> Any:
    if isinstance(value, list) and len(value) == 1 and str(value[0]).strip() == "*":
        return "*"
    return value


def create_socket_server() -> socketio.AsyncServer:
    """Creates the Socket.IO server instance."""
    sio = socketio.AsyncServer(
        async_mode="asgi",
        cors_allowed_origins=_normalize_cors_allowed_origins(settings.cors_allowed_origins),
        logger=False,
        engineio_logger=False,
    )
    register_socket_events(sio)
    return sio


def rooms_used(sio: socketio.AsyncServer) -> int:
    """Returns a lightweight estimate of active rooms."""
    try:
        manager = getattr(sio, "manager", None)
        if not manager:
            return 0
        ns_rooms = manager.rooms.get("/", {})
        count = len(ns_rooms)
        return max(count - 1, 0)
    except Exception:
        return 0


def create_fastapi_app(sio: socketio.AsyncServer) -> FastAPI:
    """Creates the FastAPI app used as the Socket.IO ASGI wrapper app."""

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await get_access_token()
        await register_server()
        server_key = get_server_key()

        task = asyncio.create_task(
            heartbeat_loop(
                server_key=server_key,
                get_rooms_used=lambda: rooms_used(sio),
            )
        )
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        return {"status": "ok"}

    return app


sio = create_socket_server()
fastapi_app = create_fastapi_app(sio)
asgi_app = socketio.ASGIApp(sio, other_asgi_app=fastapi_app)

__all__ = ["asgi_app", "fastapi_app", "sio", "create_fastapi_app", "create_socket_server"]


# uvicorn socket_server.main:asgi_app --host 0.0.0.0 --port 8001 --reload

