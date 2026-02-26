from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.socket.tbl_socket_room_members import SocketRoomMember

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("socket.members.leave")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class LeaveRoomPayload(BaseModel):
    room_id: int = Field(..., description="Room id")
    member_key: str = Field(..., description="Service instance key")
    member_type: str = Field(default="service", description="Member type")

    @field_validator("member_key", "member_type")
    @classmethod
    def validate_text(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("value is required")
        return v


class LeaveRoomCommand(BaseCommand):
    """Marks a service member as left from a room."""

    name = "socket/members/leave"
    schema = LeaveRoomPayload
    require_auth = False
    method = "post"
    type = "json"
    group = "Socket"
    auth_mode = "internal"

    async def execute(self, payload: LeaveRoomPayload) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            row: Optional[SocketRoomMember] = (
                db.query(SocketRoomMember)
                .filter(
                    and_(
                        SocketRoomMember.room_id == payload.room_id,
                        SocketRoomMember.member_type == payload.member_type,
                        SocketRoomMember.member_key == payload.member_key,
                        SocketRoomMember.left_at.is_(None),
                    )
                )
                .first()
            )
            if not row:
                raise HTTPException(status_code=404, detail="active membership not found")

            row.left_at = func.now()
            db.add(row)
            db.commit()

            return {"status": "ok", "data": {"member_id": row.id, "room_id": row.room_id}}
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            logger.exception("[socket] leave member error")
            raise
        finally:
            db.close()


__all__ = ["LeaveRoomCommand"]
