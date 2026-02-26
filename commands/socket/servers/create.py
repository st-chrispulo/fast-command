from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import urlparse

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.socket.tbl_socket_servers import SocketServer

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("socket.servers.create")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


def _normalize_host(v: str) -> str:
    s = (v or "").strip().rstrip("/")
    if not s:
        raise ValueError("host is required")

    if s.startswith("http://") or s.startswith("https://"):
        u = urlparse(s)
        if not u.hostname:
            raise ValueError("host must include a hostname")
        if not u.port:
            raise ValueError("host must include a port (e.g. http://127.0.0.1:8001)")
        return f"{u.scheme}://{u.hostname}:{u.port}"

    u = urlparse(f"http://{s}")
    if not u.hostname:
        raise ValueError("host must include a hostname")
    if not u.port:
        raise ValueError("host must include a port (e.g. 127.0.0.1:8001)")
    return f"http://{u.hostname}:{u.port}"


class CreateSocketServerPayload(BaseModel):
    server_key: str = Field(..., description="Unique server key")
    name: Optional[str] = Field(default=None, description="Display name")
    host: str = Field(..., description="Base URL or host:port (e.g. http://127.0.0.1:8001 or 127.0.0.1:8001)")
    region: Optional[str] = Field(default=None, description="Region")
    max_rooms: int = Field(default=0, description="0 means unlimited")
    is_active: bool = Field(default=True, description="Active flag")

    @field_validator("server_key")
    @classmethod
    def validate_server_key(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("server_key is required")
        return v

    @field_validator("host")
    @classmethod
    def validate_host(cls, v: str) -> str:
        return _normalize_host(v)

    @field_validator("max_rooms")
    @classmethod
    def validate_max_rooms(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_rooms must be >= 0")
        return v


class CreateSocketServerCommand(BaseCommand):
    """Creates a socket server instance."""

    name = "socket/servers/create"
    schema = CreateSocketServerPayload
    require_auth = True
    method = "post"
    type = "json"
    group = "Socket"
    auth_mode = "internal"
    internal_scopes = ["socket:servers:create"]

    async def execute(self, payload: CreateSocketServerPayload, user_id: Optional[str] = None) -> Dict[str, Any]:
        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        db = SessionLocal()
        try:
            exists = db.query(SocketServer.id).filter(SocketServer.server_key == payload.server_key).first()
            if exists:
                raise HTTPException(status_code=409, detail="server_key already exists")

            row = SocketServer(
                server_key=payload.server_key,
                name=payload.name,
                host=payload.host,
                region=payload.region,
                max_rooms=payload.max_rooms,
                is_active=payload.is_active,
            )
            db.add(row)
            db.commit()
            db.refresh(row)

            return {
                "status": "ok",
                "data": {
                    "id": row.id,
                    "server_key": row.server_key,
                    "name": row.name,
                    "host": row.host,
                    "region": row.region,
                    "is_active": row.is_active,
                    "max_rooms": int(row.max_rooms or 0),
                    "rooms_used": int(row.rooms_used or 0),
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                },
            }
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            logger.exception("[socket] create server error")
            raise
        finally:
            db.close()


__all__ = ["CreateSocketServerCommand"]
