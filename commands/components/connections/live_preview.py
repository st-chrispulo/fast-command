from __future__ import annotations

import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, desc

from app.core.settings import settings
from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.socket.tbl_socket_rooms import SocketRoom
from models.socket.tbl_socket_servers import SocketServer

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.live_preview")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class LivePreviewPayload(BaseModel):
    funnel_id: str = Field(..., description="Funnel ID (uuid)")
    preferred_region: Optional[str] = Field(default=None, description="Preferred socket server region")

    @field_validator("funnel_id")
    @classmethod
    def _validate_funnel_id(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("funnel_id is required")
        return v

    @field_validator("preferred_region")
    @classmethod
    def _validate_region(cls, v: Optional[str]) -> Optional[str]:
        s = (v or "").strip()
        return s or None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _server_is_stale(server: SocketServer) -> bool:
    last_seen = getattr(server, "last_seen_at", None)
    if not last_seen:
        return True
    stale_seconds = int(getattr(settings, "socket_server_stale_seconds", 60) or 60)
    return last_seen < (_utcnow() - timedelta(seconds=stale_seconds))


def _server_has_capacity(server: SocketServer) -> bool:
    max_rooms = int(getattr(server, "max_rooms", 0) or 0)
    if max_rooms <= 0:
        return True
    rooms_used = int(getattr(server, "rooms_used", 0) or 0)
    return rooms_used < max_rooms


def _server_score(server: SocketServer) -> int:
    return int(getattr(server, "rooms_used", 0) or 0)


def _pick_server(db, preferred_region: Optional[str]) -> SocketServer:
    base = db.query(SocketServer).filter(SocketServer.is_active.is_(True))
    rows = base.all()
    rows = [s for s in rows if not _server_is_stale(s) and _server_has_capacity(s)]

    if not rows:
        raise HTTPException(status_code=503, detail="No available socket servers")

    if preferred_region:
        region_rows = [s for s in rows if (getattr(s, "region", None) or "").strip() == preferred_region]
        if region_rows:
            return sorted(region_rows, key=_server_score)[0]

    return sorted(rows, key=_server_score)[0]


def _room_key() -> str:
    return f"preview_{secrets.token_urlsafe(18)}"


def _join_token() -> str:
    return secrets.token_urlsafe(32)


def _join_token_hash(token: str) -> str:
    secret = (getattr(settings, "socket_room_join_token_secret", "") or "").strip()
    if not secret:
        raise HTTPException(status_code=500, detail="socket_room_join_token_secret not configured")
    mac = hmac.new(secret.encode("utf-8"), token.encode("utf-8"), digestmod="sha256")
    return mac.hexdigest()


def _socket_base_url(server: SocketServer) -> str:
    host = (getattr(server, "host", None) or "").strip()
    if not host:
        raise HTTPException(status_code=500, detail="Socket server host is missing")
    if host.startswith("http://") or host.startswith("https://"):
        return host.rstrip("/")
    return f"http://{host}".rstrip("/")


def _extract_scope_value(room: SocketRoom, key: str) -> Optional[str]:
    scope = getattr(room, "scope_json", None) or {}
    v = scope.get(key)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _find_existing_preview_room(db, funnel_id: str, user_id: str) -> Optional[SocketRoom]:
    q = (
        db.query(SocketRoom)
        .filter(
            and_(
                SocketRoom.is_active.is_(True),
                SocketRoom.room_type == "preview",
            )
        )
        .order_by(desc(getattr(SocketRoom, "created_at", SocketRoom.id)))
    )

    rooms = q.all()
    for r in rooms:
        if _extract_scope_value(r, "funnel_id") != funnel_id:
            continue
        if _extract_scope_value(r, "requested_by") != str(user_id):
            continue
        return r
    return None


def _get_server_by_id(db, server_id: Any) -> Optional[SocketServer]:
    if not server_id:
        return None
    return db.query(SocketServer).filter(SocketServer.id == server_id).first()


class LivePreviewCommand(BaseCommand):
    """Creates or reuses a live preview room and returns connection details."""

    name = "components/connections/preview"
    schema = LivePreviewPayload
    require_auth = True
    method = "post"
    type = "json"
    group = "Funnel"
    auth_mode = "user"

    async def execute(self, payload: LivePreviewPayload, user_id: Optional[str] = None) -> Dict[str, Any]:
        if not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized")

        db = SessionLocal()
        try:
            existing = _find_existing_preview_room(db, payload.funnel_id, str(user_id))
            if existing:
                server = _get_server_by_id(db, getattr(existing, "server_id", None))
                if server and getattr(server, "is_active", False) and (not _server_is_stale(server)):
                    join_token = _join_token()
                    existing.join_token_hash = _join_token_hash(join_token)
                    db.commit()
                    db.refresh(existing)

                    base_url = _socket_base_url(server)
                    room_key = getattr(existing, "room_key", None) or _room_key()

                    if not getattr(existing, "room_key", None):
                        existing.room_key = room_key
                        db.commit()
                        db.refresh(existing)

                    return {
                        "status": "ok",
                        "data": {
                            "server": {
                                "id": server.id,
                                "server_key": getattr(server, "server_key", None),
                                "name": getattr(server, "name", None),
                                "host": getattr(server, "host", None),
                                "region": getattr(server, "region", None),
                                "max_rooms": int(getattr(server, "max_rooms", 0) or 0),
                                "rooms_used": int(getattr(server, "rooms_used", 0) or 0),
                                "last_seen_at": getattr(server, "last_seen_at", None).isoformat()
                                if getattr(server, "last_seen_at", None)
                                else None,
                            },
                            "room": {
                                "id": existing.id,
                                "room_key": room_key,
                                "room_type": "preview",
                                "scope": existing.scope_json or {},
                            },
                            "socket": {
                                "base_url": base_url,
                                "namespace": "/",
                                "join": {
                                    "room_key": room_key,
                                    "join_token": join_token,
                                },
                            },
                            "bootstrap": {
                                "funnel_id": payload.funnel_id,
                                "room_key": room_key,
                            },
                        },
                    }

                existing.is_active = False
                db.commit()

            server = _pick_server(db, payload.preferred_region)

            room_key = _room_key()
            join_token = _join_token()
            join_hash = _join_token_hash(join_token)

            room = SocketRoom(
                server_id=server.id,
                room_key=room_key,
                room_type="preview",
                join_token_hash=join_hash,
                scope_json={
                    "purpose": "comp_connections_preview",
                    "funnel_id": payload.funnel_id,
                    "requested_by": str(user_id),
                },
                is_active=True,
            )

            db.add(room)

            if hasattr(server, "rooms_used"):
                server.rooms_used = int(getattr(server, "rooms_used", 0) or 0) + 1

            db.commit()
            db.refresh(room)

            base_url = _socket_base_url(server)

            return {
                "status": "ok",
                "data": {
                    "server": {
                        "id": server.id,
                        "server_key": getattr(server, "server_key", None),
                        "name": getattr(server, "name", None),
                        "host": getattr(server, "host", None),
                        "region": getattr(server, "region", None),
                        "max_rooms": int(getattr(server, "max_rooms", 0) or 0),
                        "rooms_used": int(getattr(server, "rooms_used", 0) or 0),
                        "last_seen_at": getattr(server, "last_seen_at", None).isoformat()
                        if getattr(server, "last_seen_at", None)
                        else None,
                    },
                    "room": {
                        "id": room.id,
                        "room_key": room_key,
                        "room_type": "preview",
                        "scope": room.scope_json or {},
                    },
                    "socket": {
                        "base_url": base_url,
                        "namespace": "/",
                        "join": {
                            "room_key": room_key,
                            "join_token": join_token,
                        },
                    },
                    "bootstrap": {
                        "funnel_id": payload.funnel_id,
                        "room_key": room_key,
                    },
                },
            }
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            logger.exception("[live_preview] failed")
            raise
        finally:
            db.close()


__all__ = ["LivePreviewCommand"]
