# commands/components/content/delete.py
import time
from typing import Optional

from pydantic import BaseModel, field_validator
from fastapi import HTTPException

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from models.components.tbl_comp_contents import CompContent

# Prefer your app logger if available; fallback to stdlib
try:
    from logger import logger
except Exception:
    import logging as _logging
    logger = _logging.getLogger("delete_comp_content")
    if not logger.handlers:
        handler = _logging.StreamHandler()
        handler.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(_logging.DEBUG)


class DeleteCompContentPayload(BaseModel):
    # Accept string to avoid strict UUID parsing errors from Swagger/UI
    id: str

    @field_validator("id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("id is required")
        return v


class DeleteCompContentCommand(BaseCommand):
    """
    Hard-deletes a CompContent row from the database.
    NOTE: Does NOT delete GCS objects. Handle storage cleanup separately.
    """

    name = "components/contents/delete"
    schema = DeleteCompContentPayload
    require_auth = True
    method = "delete"     # keep consistent with your other commands; change to "delete" if your router supports it
    type = "json"       # no files

    async def execute(self, payload: DeleteCompContentPayload, user_id: Optional[str] = None):
        start_t = time.monotonic()
        logger.info("[comp_contents/delete] start id=%s user_id=%s", payload.id, user_id)

        # Auth guard: require user context (either injected by framework or explicit)
        current_uid = getattr(self, "user_id", None) or getattr(self, "actor_id", None)
        if self.require_auth and current_uid is None:
            logger.warning("[comp_contents/delete] unauthorized - no user context")
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        db = SessionLocal()
        try:
            # Fetch row
            row = db.query(CompContent).get(payload.id)
            if not row:
                logger.info("[comp_contents/delete] not found id=%s", payload.id)
                raise HTTPException(status_code=404, detail="Content not found")

            # Optionally capture minimal info before deletion (for response/audit)
            deleted_id = str(row.id)
            deleted_name = row.name

            # Hard delete
            db.delete(row)
            db.commit()

            elapsed = time.monotonic() - start_t
            logger.info("[comp_contents/delete] deleted id=%s name=%s elapsed=%.3fs", deleted_id, deleted_name, elapsed)

            return {
                "status": "ok",
                "deleted": True,
                "id": deleted_id,
                "name": deleted_name,
            }

        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            logger.exception("[comp_contents/delete] error - rolling back: %s", e)
            db.rollback()
            raise HTTPException(status_code=500, detail="Failed to delete content")
        finally:
            db.close()
