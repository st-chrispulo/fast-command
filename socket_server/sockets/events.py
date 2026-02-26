from __future__ import annotations

from typing import Any, Dict, Optional

import socketio

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("socket_server.sockets.events")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


def register_socket_events(sio: socketio.AsyncServer) -> None:
    """Registers Socket.IO events for the server."""

    @sio.event
    async def connect(sid: str, environ: Dict[str, Any], auth: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        origin = None
        try:
            origin = environ.get("HTTP_ORIGIN")
        except Exception:
            origin = None

        logger.info("[socket] connect sid=%s origin=%s", sid, origin)
        return {"ok": True, "sid": sid}

    @sio.event
    async def disconnect(sid: str) -> None:
        logger.info("[socket] disconnect sid=%s", sid)

    @sio.event
    async def ping(sid: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {"ok": True, "sid": sid, "payload": payload or {}}

    @sio.event
    async def echo(sid: str, payload: Any = None) -> Dict[str, Any]:
        return {"ok": True, "sid": sid, "payload": payload}

    @sio.event
    async def join_room(sid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        room = str((payload or {}).get("room", "")).strip()
        if not room:
            return {"ok": False, "error": "room_required"}
        try:
            await sio.enter_room(sid, room)
            logger.info("[socket] join_room sid=%s room=%s", sid, room)
            return {"ok": True, "room": room}
        except Exception:
            logger.exception("[socket] join_room failed sid=%s room=%s", sid, room)
            return {"ok": False, "error": "join_failed", "room": room}

    @sio.event
    async def leave_room(sid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        room = str((payload or {}).get("room", "")).strip()
        if not room:
            return {"ok": False, "error": "room_required"}
        try:
            await sio.leave_room(sid, room)
            logger.info("[socket] leave_room sid=%s room=%s", sid, room)
            return {"ok": True, "room": room}
        except Exception:
            logger.exception("[socket] leave_room failed sid=%s room=%s", sid, room)
            return {"ok": False, "error": "leave_failed", "room": room}

    @sio.event
    async def room_emit(sid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        room = str((payload or {}).get("room", "")).strip()
        event = str((payload or {}).get("event", "")).strip()
        data = (payload or {}).get("data")

        if not room:
            return {"ok": False, "error": "room_required"}
        if not event:
            return {"ok": False, "error": "event_required"}

        try:
            await sio.emit(event, data, room=room)
            return {"ok": True, "room": room, "event": event}
        except Exception:
            logger.exception("[socket] room_emit failed sid=%s room=%s event=%s", sid, room, event)
            return {"ok": False, "error": "emit_failed", "room": room, "event": event}


__all__ = ["register_socket_events"]
