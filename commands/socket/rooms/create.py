from __future__ import annotations

import hashlib
import secrets
from typing import Any, Dict, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.socket.tbl_socket_rooms import SocketRoom
from models.socket.tbl_socket_servers import SocketServer

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("socket.rooms.create")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class CreateSocketRoomPayload(BaseModel):
    server_key: str = Field(..., description="Server key")
    room_key: str = Field(..., description="Room key")
    room_type: str = Field(default="custom", description="Room type")
    scope_json: Optional[dict] = Field(default=None, description="Room scope metadata")
    is_active: bool = Field(default=True)

    @field_validator("server_key", "room_key", "room_type")
    @classmethod
    def validate_text(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("value is required")
        return v


class CreateSocketRoomCommand(BaseCommand):
    """Creates a room under a server and returns the join token once."""

    name = "socket/rooms/create"
    schema = CreateSocketRoomPayload
    require_auth = True
    method = "post"
    type = "json"
    group = "Socket"
    auth_mode = "internal"

    async def execute(
        self,
        payload: CreateSocketRoomPayload,
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

            if not bool(server.is_active):
                raise HTTPException(status_code=400, detail="server is not active")

            max_rooms = int(server.max_rooms or 0)
            rooms_used = int(server.rooms_used or 0)
            if max_rooms != 0 and rooms_used >= max_rooms:
                raise HTTPException(status_code=409, detail="server has no available room capacity")

            exists = (
                db.query(SocketRoom.id)
                .filter(SocketRoom.server_id == server.id, SocketRoom.room_key == payload.room_key)
                .first()
            )
            if exists:
                raise HTTPException(status_code=409, detail="room_key already exists on this server")

            raw_token = secrets.token_urlsafe(48)
            token_hash = _hash_token(raw_token)

            row = SocketRoom(
                server_id=server.id,
                room_key=payload.room_key,
                room_type=payload.room_type,
                join_token_hash=token_hash,
                scope_json=payload.scope_json,
                is_active=payload.is_active,
            )

            server.rooms_used = int(server.rooms_used or 0) + 1

            db.add(row)
            db.add(server)
            db.commit()
            db.refresh(row)

            return {
                "status": "ok",
                "data": {
                    "id": row.id,
                    "server_id": row.server_id,
                    "room_key": row.room_key,
                    "room_type": row.room_type,
                    "scope_json": row.scope_json,
                    "is_active": row.is_active,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                },
                "join_token": raw_token,
            }
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            logger.exception("[socket] create room error")
            raise
        finally:
            db.close()


__all__ = ["CreateSocketRoomCommand"]
