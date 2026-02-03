from __future__ import annotations

import time
from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.tbl_funnels import Funnel

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.funnels.delete")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class DeleteFunnelsPayload(BaseModel):
    ids: List[UUID] = Field(..., description="Funnel ids to delete")

    @field_validator("ids")
    @classmethod
    def validate_ids(cls, v: List[UUID]) -> List[UUID]:
        if not v:
            raise ValueError("ids array is required and cannot be empty")

        cleaned = [x for x in v if x is not None]
        if not cleaned:
            raise ValueError("ids array cannot be all null values")

        uniq: List[UUID] = []
        seen = set()
        for x in cleaned:
            if x not in seen:
                seen.add(x)
                uniq.append(x)

        if not uniq:
            raise ValueError("ids array is required and cannot be empty")

        return uniq


class DeleteFunnelsCommand(BaseCommand):
    name = "components/funnels/delete"
    schema = DeleteFunnelsPayload
    require_auth = True
    method = "delete"
    type = "json"
    group = "Funnel"

    async def execute(self, payload: DeleteFunnelsPayload, user_id: Optional[str] = None):
        t0 = time.monotonic()

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        ids = payload.ids
        logger.info("[funnels] bulk delete start ids=%s user_id=%s", [str(i) for i in ids], user_id)

        db = SessionLocal()
        try:
            rows = db.query(Funnel).filter(Funnel.id.in_(ids)).all()
            if not rows:
                raise HTTPException(status_code=404, detail="No funnels found for given ids")

            deleted_ids = [str(r.id) for r in rows]
            for r in rows:
                db.delete(r)

            db.commit()

            logger.info(
                "[funnels] bulk delete ok count=%d user_id=%s elapsed=%.3fs",
                len(deleted_ids),
                user_id,
                time.monotonic() - t0,
            )

            return {
                "status": "ok",
                "data": {"deleted_count": len(deleted_ids), "deleted_ids": deleted_ids},
            }
        except HTTPException:
            db.rollback()
            logger.exception("[funnels] HTTPException during bulk delete")
            raise
        except Exception:
            db.rollback()
            logger.exception("[funnels] DB error during bulk delete")
            raise
        finally:
            db.close()
