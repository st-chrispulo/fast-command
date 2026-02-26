from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.socket.tbl_socket_rooms import SocketRoom

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("socket.rooms.validate_token")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ValidateRoomTokenPayload(BaseModel):
    join_token: str = Field(..., description="Room join token")

    @field_validator("join_token")
    @classmethod
    def validate_join_token(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("join_token is required")
        return v


class ValidateRoomTokenCommand(BaseCommand):
    """Validates a join token and returns room details."""

    name = "socket/rooms/validate_token"
    schema = ValidateRoomTokenPayload
    require_auth = False
    method = "post"
    type = "json"
    group = "Socket"
    auth_mode = "internal"

    async def execute(self, payload: ValidateRoomTokenPayload) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            token_hash = _hash_token(payload.join_token)
            room: Optional[SocketRoom] = (
                db.query(SocketRoom).filter(SocketRoom.join_token_hash == token_hash).first()
            )
            if not room:
                raise HTTPException(status_code=401, detail="invalid token")

            if not bool(room.is_active):
                raise HTTPException(status_code=403, detail="room is not active")

            return {
                "status": "ok",
                "data": {
                    "id": room.id,
                    "server_id": room.server_id,
                    "room_key": room.room_key,
                    "room_type": room.room_type,
                    "scope_json": room.scope_json,
                    "is_active": room.is_active,
                },
            }
        except HTTPException:
            raise
        except Exception:
            logger.exception("[socket] validate token error")
            raise
        finally:
            db.close()


__all__ = ["ValidateRoomTokenCommand"]
