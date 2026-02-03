from __future__ import annotations

import time
from typing import List, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.components.tbl_comp_connections import CompConnection

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.connections.delete")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


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

    async def execute(self, payload: DeleteCompConnectionsPayload, user_id: Optional[str] = None) -> dict:
        t0 = time.monotonic()
        logger.info("[connections] delete start ids=%s user_id=%s", payload.ids, user_id)

        db = SessionLocal()
        try:
            rows = db.query(CompConnection).filter(CompConnection.id.in_(payload.ids)).all()
            if not rows:
                raise HTTPException(status_code=404, detail="No matching connections found")

            found_ids = {str(r.id) for r in rows}
            not_found = [i for i in payload.ids if i not in found_ids]

            for r in rows:
                db.delete(r)
            db.commit()

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
                "ids_deleted": sorted(found_ids),
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
