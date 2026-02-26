from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.socket.tbl_socket_servers import SocketServer

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("socket.servers.heartbeat")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class HeartbeatServerPayload(BaseModel):
    server_key: str = Field(..., description="Unique server key")
    rooms_used: Optional[int] = Field(default=None, ge=0, description="Current used rooms count")

    @field_validator("server_key")
    @classmethod
    def validate_server_key(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("server_key is required")
        return v


class HeartbeatServerCommand(BaseCommand):
    """Updates liveness fields for a socket server instance."""

    name = "socket/servers/heartbeat"
    schema = HeartbeatServerPayload
    require_auth = False
    method = "post"
    type = "json"
    group = "Socket"
    auth_mode = "internal"

    async def execute(self, payload: HeartbeatServerPayload) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            row: Optional[SocketServer] = (
                db.query(SocketServer)
                .filter(SocketServer.server_key == payload.server_key)
                .first()
            )
            if not row:
                raise HTTPException(status_code=404, detail="server_key not found")

            if payload.rooms_used is not None:
                row.rooms_used = int(payload.rooms_used)

            row.last_seen_at = func.now()
            db.add(row)
            db.commit()

            return {"status": "ok", "data": {"server_key": row.server_key}}
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            logger.exception("[socket] heartbeat error")
            raise
        finally:
            db.close()


__all__ = ["HeartbeatServerCommand"]
