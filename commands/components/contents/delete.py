# commands/components/content/delete.py
import time
from typing import Optional, List

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
    # Accept a list of string IDs (kept as str to avoid strict UUID parsing issues in Swagger/UI)
    ids: List[str]

    @field_validator("ids")
    @classmethod
    def _validate_ids(cls, v: List[str]) -> List[str]:
        if not isinstance(v, list):
            raise ValueError("ids must be a list")
        cleaned = [str(x).strip() for x in v if str(x or "").strip()]
        if not cleaned:
            raise ValueError("ids cannot be empty")
        # de-dup while preserving order
        seen = set()
        uniq = []
        for x in cleaned:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq


class DeleteCompContentCommand(BaseCommand):
    """
    Hard-deletes one or more CompContent rows from the database.
    NOTE: Does NOT delete GCS objects. Handle storage cleanup separately.
    """

    name = "components/contents/delete"
    schema = DeleteCompContentPayload
    require_auth = True
    method = "delete"   # keep consistent with your router
    type = "json"
    group = "Content"

    async def execute(self, payload: DeleteCompContentPayload, user_id: Optional[str] = None):
        start_t = time.monotonic()
        logger.info("[comp_contents/delete] start ids=%s user_id=%s", payload.ids, user_id)

        db = SessionLocal()
        try:
            # Fetch rows that exist
            rows = (
                db.query(CompContent)
                .filter(CompContent.id.in_(payload.ids))
                .all()
            )

            if not rows:
                logger.info("[comp_contents/delete] none found ids=%s", payload.ids)
                raise HTTPException(status_code=404, detail="No matching content found")

            # Track found IDs for response
            found_ids = {str(r.id) for r in rows}
            not_found = [i for i in payload.ids if i not in found_ids]

            # Hard delete found rows
            for r in rows:
                db.delete(r)
            db.commit()

            elapsed = time.monotonic() - start_t
            logger.info(
                "[comp_contents/delete] deleted_count=%d not_found=%d elapsed=%.3fs",
                len(found_ids), len(not_found), elapsed
            )

            return {
                "status": "ok",
                "deleted": True,
                "deleted_count": len(found_ids),
                "ids_deleted": sorted(found_ids),
                "not_found": not_found,  # IDs requested but not present
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
