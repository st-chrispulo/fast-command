from __future__ import annotations

import time
from typing import List, Optional, Set
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from integrations.socket.publisher import get_socket_publisher
from models.components.tbl_comp_connections import CompConnection
from models.socket.tbl_socket_rooms import SocketRoom
from models.socket.tbl_socket_servers import SocketServer

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.connections.delete")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


def _socket_base_url(server: SocketServer) -> str:
    host = (getattr(server, "host", None) or "").strip()
    if not host:
        return ""
    if host.startswith("http://") or host.startswith("https://"):
        return host.rstrip("/")
    return f"http://{host}".rstrip("/")


def _extract_funnel_ids(rows: List[CompConnection]) -> Set[str]:
    out: Set[str] = set()
    for r in rows:
        fid = getattr(r, "funnel_id", None)
        if fid is not None:
            out.add(str(fid))
    return out


class DeleteCompConnectionsPayload(BaseModel):
    """Delete one or more connection rows by id."""

    ids: List[str] = Field(description="Connection ids to delete")

    @field_validator("ids")
    @classmethod
    def validate_ids(cls, v: List[str]) -> List[str]:
        if not isinstance(v, list):
            raise ValueError("ids must be a list")

        cleaned = [str(x).strip() for x in v if str(x or "").strip()]
        if not cleaned:
            raise ValueError("ids cannot be empty")

        seen = set()
        uniq: List[str] = []
        for x in cleaned:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq


class DeleteCompConnectionsCommand(BaseCommand):
    """Hard-delete one or more CompConnection rows from the database."""

    name = "components/connections/delete"
    schema = DeleteCompConnectionsPayload
    require_auth = True
    method = "delete"
    type = "json"
    group = "Funnel"

    async def _emit_deleted(self, db, funnel_id: str, user_id: Optional[str], ids_deleted: List[str]) -> None:
        publisher = get_socket_publisher()

        rooms = (
            db.query(SocketRoom)
            .filter(
                and_(
                    SocketRoom.is_active.is_(True),
                    SocketRoom.room_type == "preview",
                    SocketRoom.scope_json["funnel_id"].astext == funnel_id,
                )
            )
            .all()
        )
        if not rooms:
            return

        server_ids = {getattr(r, "server_id", None) for r in rooms if getattr(r, "server_id", None)}
        if not server_ids:
            return

        servers = db.query(SocketServer).filter(SocketServer.id.in_(list(server_ids))).all()
        servers_by_id = {s.id: s for s in servers}

        payload_out = {
            "event": "connections.deleted",
            "description": "Connection deleted",
            "funnel_id": funnel_id,
            "user_id": str(user_id) if user_id else None,
            "data": {"ids_deleted": ids_deleted},
        }

        for r in rooms:
            server = servers_by_id.get(getattr(r, "server_id", None))
            if not server or not getattr(server, "is_active", False):
                continue

            base_url = _socket_base_url(server)
            room_key = (getattr(r, "room_key", None) or "").strip()
            if not base_url or not room_key:
                continue

            await publisher.emit_to_room(base_url, room_key, "connections.delete", payload_out)

    async def execute(self, payload: DeleteCompConnectionsPayload, user_id: Optional[str] = None) -> dict:
        t0 = time.monotonic()
        logger.info("[connections] delete start ids=%s user_id=%s", payload.ids, user_id)

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        db = SessionLocal()
        try:
            rows = db.query(CompConnection).filter(CompConnection.id.in_(payload.ids)).all()
            if not rows:
                raise HTTPException(status_code=404, detail="No matching connections found")

            found_ids = {str(r.id) for r in rows}
            not_found = [i for i in payload.ids if i not in found_ids]
            funnel_ids = _extract_funnel_ids(rows)

            for r in rows:
                db.delete(r)
            db.commit()

            ids_deleted = sorted(found_ids)

            for fid in funnel_ids:
                try:
                    await self._emit_deleted(db, fid, user_id, ids_deleted)
                except Exception:
                    logger.exception("[connections] delete emit failed funnel_id=%s", fid)

            logger.info(
                "[connections] delete ok deleted_count=%d not_found=%d perf_ms=%.2f",
                len(found_ids),
                len(not_found),
                (time.monotonic() - t0) * 1000,
            )

            return {
                "status": "ok",
                "deleted": True,
                "deleted_count": len(found_ids),
                "ids_deleted": ids_deleted,
                "not_found": not_found,
            }
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            logger.exception("[connections] delete failed")
            raise HTTPException(status_code=500, detail="Failed to delete connections")
        finally:
            db.close()


__all__ = ["DeleteCompConnectionsCommand"]
