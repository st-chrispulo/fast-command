from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.socket.tbl_socket_room_members import SocketRoomMember
from models.socket.tbl_socket_rooms import SocketRoom

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("socket.members.join")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class JoinRoomPayload(BaseModel):
    join_token: str = Field(..., description="Room join token")
    member_key: str = Field(..., description="Service instance key")
    member_type: str = Field(default="service", description="Member type")
    meta_json: Optional[dict] = Field(default=None)

    @field_validator("join_token", "member_key", "member_type")
    @classmethod
    def validate_text(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("value is required")
        return v


class JoinRoomCommand(BaseCommand):
    """Joins a service into a room using the room token."""

    name = "socket/members/join"
    schema = JoinRoomPayload
    require_auth = False
    method = "post"
    type = "json"
    group = "Socket"
    auth_mode = "internal"

    async def execute(self, payload: JoinRoomPayload) -> Dict[str, Any]:
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

            row: Optional[SocketRoomMember] = (
                db.query(SocketRoomMember)
                .filter(
                    SocketRoomMember.room_id == room.id,
                    SocketRoomMember.member_type == payload.member_type,
                    SocketRoomMember.member_key == payload.member_key,
                    SocketRoomMember.left_at.is_(None),
                )
                .first()
            )

            if row:
                if payload.meta_json is not None:
                    row.meta_json = payload.meta_json
                db.add(row)
                db.commit()
                db.refresh(row)
                return {
                    "status": "ok",
                    "data": {
                        "member_id": row.id,
                        "room_id": room.id,
                        "server_id": room.server_id,
                        "room_key": room.room_key,
                    },
                }

            row = SocketRoomMember(
                room_id=room.id,
                member_type=payload.member_type,
                member_key=payload.member_key,
                meta_json=payload.meta_json,
            )
            db.add(row)
            db.commit()
            db.refresh(row)

            return {
                "status": "ok",
                "data": {
                    "member_id": row.id,
                    "room_id": room.id,
                    "server_id": room.server_id,
                    "room_key": room.room_key,
                },
            }
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            logger.exception("[socket] join member error")
            raise
        finally:
            db.close()


__all__ = ["JoinRoomCommand"]
