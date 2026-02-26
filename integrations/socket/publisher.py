from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import socketio

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("integrations.socket.publisher")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class SocketPublisher:
    """Publishes events to Socket.IO servers."""

    def __init__(self, token: Optional[str] = None, namespace: str = "/") -> None:
        self._token = (token or "").strip() or None
        self._namespace = (namespace or "/").strip() or "/"
        self._clients: Dict[str, socketio.AsyncClient] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock_for(self, base_url: str) -> asyncio.Lock:
        lock = self._locks.get(base_url)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[base_url] = lock
        return lock

    def _client_for(self, base_url: str) -> socketio.AsyncClient:
        client = self._clients.get(base_url)
        if client is None:
            client = socketio.AsyncClient(reconnection=True, logger=False, engineio_logger=False)
            self._clients[base_url] = client
        return client

    async def ensure_connected(self, base_url: str) -> bool:
        base_url = (base_url or "").strip().rstrip("/")
        if not base_url:
            return False

        sio = self._client_for(base_url)
        if sio.connected:
            return True

        async with self._lock_for(base_url):
            if sio.connected:
                return True

            headers: Dict[str, str] = {}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"

            try:
                await sio.connect(
                    base_url,
                    headers=headers,
                    namespaces=[self._namespace],
                    transports=["websocket", "polling"],
                )
                return True
            except Exception:
                logger.exception("[socket] connect failed base_url=%s ns=%s", base_url, self._namespace)
                return False

    async def emit_to_room(self, base_url: str, room_key: str, event: str, payload: Dict[str, Any]) -> None:
        base_url = (base_url or "").strip().rstrip("/")
        room_key = (room_key or "").strip()
        event = (event or "").strip()
        if not base_url or not room_key or not event:
            return

        ok = await self.ensure_connected(base_url)
        if not ok:
            return

        sio = self._client_for(base_url)

        msg = {
            "room": room_key,
            "event": event,
            "data": payload or {},
        }

        try:
            await sio.emit("room_emit", msg, namespace=self._namespace)
        except Exception:
            logger.exception("[socket] room_emit failed base_url=%s room=%s event=%s", base_url, room_key, event)


_publisher: Optional[SocketPublisher] = None


def init_socket_publisher(token: Optional[str] = None, namespace: str = "/") -> SocketPublisher:
    global _publisher
    _publisher = SocketPublisher(token=token, namespace=namespace)
    return _publisher


def get_socket_publisher() -> SocketPublisher:
    global _publisher
    if _publisher is None:
        _publisher = SocketPublisher(token=None, namespace="/")
    return _publisher


__all__ = ["SocketPublisher", "init_socket_publisher", "get_socket_publisher"]
