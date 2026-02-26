from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.socket.tbl_socket_rooms import SocketRoom
from models.socket.tbl_socket_servers import SocketServer

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("socket.rooms.list")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class ListSocketRoomsPayload(BaseModel):
    server_key: str = Field(..., description="Server key")
    is_active: Optional[bool] = Field(default=None)
    limit: int = Field(default=200, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)

    @field_validator("server_key")
    @classmethod
    def validate_server_key(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("server_key is required")
        return v


class ListSocketRoomsCommand(BaseCommand):
    """Lists rooms for a server."""

    name = "socket/rooms/list"
    schema = ListSocketRoomsPayload
    require_auth = True
    method = "post"
    type = "json"
    group = "Socket"
    auth_mode = "internal"

    async def execute(
        self,
        payload: ListSocketRoomsPayload,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        db = SessionLocal()
        try:
            server: Optional[SocketServer] = (
                db.query(SocketServer).filter(SocketServer.server_key == payload.server_key).first()
            )
            if not server:
                raise HTTPException(status_code=404, detail="server not found")

            q = db.query(SocketRoom).filter(SocketRoom.server_id == server.id)
            if payload.is_active is not None:
                q = q.filter(SocketRoom.is_active == payload.is_active)

            rows: List[SocketRoom] = (
                q.order_by(SocketRoom.created_at.desc()).limit(payload.limit).offset(payload.offset).all()
            )

            data = []
            for r in rows:
                data.append(
                    {
                        "id": r.id,
                        "server_id": r.server_id,
                        "room_key": r.room_key,
                        "room_type": r.room_type,
                        "scope_json": r.scope_json,
                        "is_active": r.is_active,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                    }
                )

            return {"status": "ok", "data": data}
        except HTTPException:
            raise
        except Exception:
            logger.exception("[socket] list rooms error")
            raise
        finally:
            db.close()


__all__ = ["ListSocketRoomsCommand"]
