import time
from typing import Optional, List
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, field_validator

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from models.tbl_projects import Project

# Prefer your app logger if available; fallback to stdlib
try:
    from logger import logger
except Exception:
    import logging as _logging
    logger = _logging.getLogger("projects_delete")
    if not logger.handlers:
        handler = _logging.StreamHandler()
        handler.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(_logging.DEBUG)


class DeleteProjectsPayload(BaseModel):
    # bulk delete: one or more IDs
    ids: List[UUID]

    @field_validator("ids")
    @classmethod
    def _ids_not_empty(cls, v):
        if not v:
            raise ValueError("ids must contain at least one id")
        return v


class DeleteProjectsCommand(BaseCommand):
    """
    Deletes one or more project rows by id.
    """
    name = "projects/delete"
    schema = DeleteProjectsPayload
    require_auth = True
    method = "delete"
    group = "Project"

    async def execute(
        self,
        payload: DeleteProjectsPayload,
        user_id: Optional[str] = None,
    ):
        t0 = time.monotonic()
        logger.info(
            "[projects] delete start ids=%s user_id=%s",
            [str(i) for i in payload.ids],
            user_id,
        )

        db = SessionLocal()
        try:
            if self.require_auth and not user_id:
                raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

            q = db.query(Project).filter(Project.id.in_(payload.ids))
            rows: List[Project] = q.all()

            if not rows:
                raise HTTPException(status_code=404, detail="No matching projects found")

            deleted_ids = [str(r.id) for r in rows]

            for r in rows:
                db.delete(r)
            db.commit()

            logger.info(
                "[projects] delete ok count=%d user_id=%s dt=%.3fs",
                len(deleted_ids),
                user_id,
                time.monotonic() - t0,
            )

            return {
                "status": "ok",
                "deleted_ids": deleted_ids,
            }

        except HTTPException:
            db.rollback()
            logger.exception("[projects] delete HTTPException")
            raise
        except Exception:
            db.rollback()
            logger.exception("[projects] DB error during delete")
            raise
        finally:
            db.close()
