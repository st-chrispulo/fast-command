import time
from typing import Optional, List
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, field_validator

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from models.tbl_funnels import Funnel

# Prefer your app logger if available; fallback to stdlib
try:
    from logger import logger
except Exception:
    import logging as _logging
    logger = _logging.getLogger("funnels_delete")
    if not logger.handlers:
        handler = _logging.StreamHandler()
        handler.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(_logging.DEBUG)


class DeleteFunnelsPayload(BaseModel):
    ids: List[UUID]

    @field_validator("ids")
    @classmethod
    def _ids_not_empty(cls, v: List[UUID]) -> List[UUID]:
        if not v:
            raise ValueError("ids array is required and cannot be empty")
        # Ensure no null-ish entries
        cleaned = [item for item in v if item is not None]
        if not cleaned:
            raise ValueError("ids array cannot be all null values")
        return cleaned


class DeleteFunnelsCommand(BaseCommand):
    """
    Deletes multiple Funnels by IDs.
    """
    name = "components/funnels/delete"
    schema = DeleteFunnelsPayload
    require_auth = True
    method = "delete"
    type = "json"
    group = "Funnel"

    async def execute(
        self,
        payload: DeleteFunnelsPayload,
        user_id: Optional[str] = None,
    ):
        t0 = time.monotonic()
        logger.info(
            "[funnels] bulk delete start ids=%s user_id=%s",
            [str(i) for i in payload.ids],
            user_id,
        )

        db = SessionLocal()
        try:
            if self.require_auth and not user_id:
                raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

            q = db.query(Funnel).filter(Funnel.id.in_(payload.ids))
            rows = q.all()

            if not rows:
                # nothing found for any id
                raise HTTPException(status_code=404, detail="No funnels found for given ids")

            deleted_ids: List[str] = [str(r.id) for r in rows]

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
                "data": {
                    "deleted_count": len(deleted_ids),
                    "deleted_ids": deleted_ids,
                },
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
