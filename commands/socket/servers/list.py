from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.socket.tbl_socket_servers import SocketServer

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("socket.servers.list")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class ListSocketServersPayload(BaseModel):
    is_active: Optional[bool] = Field(default=None, description="Filter active status")
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class ListSocketServersCommand(BaseCommand):
    """Lists socket servers."""

    name = "socket/servers/list"
    schema = ListSocketServersPayload
    require_auth = True
    method = "post"
    type = "json"
    group = "Socket"
    auth_mode = "internal"

    async def execute(
        self,
        payload: ListSocketServersPayload,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        db = SessionLocal()
        try:
            q = db.query(SocketServer)
            if payload.is_active is not None:
                q = q.filter(SocketServer.is_active == payload.is_active)

            rows: List[SocketServer] = (
                q.order_by(SocketServer.created_at.desc())
                .limit(payload.limit)
                .offset(payload.offset)
                .all()
            )

            data = []
            for r in rows:
                max_rooms = int(r.max_rooms or 0)
                rooms_used = int(r.rooms_used or 0)
                available_rooms = None if max_rooms == 0 else max(0, max_rooms - rooms_used)

                data.append(
                    {
                        "id": r.id,
                        "server_key": r.server_key,
                        "name": r.name,
                        "host": r.host,
                        "region": r.region,
                        "is_active": r.is_active,
                        "max_rooms": max_rooms,
                        "rooms_used": rooms_used,
                        "available_rooms": available_rooms,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                    }
                )

            return {"status": "ok", "data": data}
        except Exception:
            logger.exception("[socket] list servers error")
            raise
        finally:
            db.close()


__all__ = ["ListSocketServersCommand"]
