from __future__ import annotations

import time
from typing import List, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.components.tbl_comp_layouts import CompLayout

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.layouts.delete")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class DeleteCompLayoutPayload(BaseModel):
    """Delete layouts by id."""

    ids: List[str] = Field(description="Layout ids to delete")

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


class DeleteCompLayoutCommand(BaseCommand):
    """Hard-delete CompLayout rows with a utility group guard."""

    name = "components/layouts/delete"
    schema = DeleteCompLayoutPayload
    require_auth = True
    method = "delete"
    type = "json"
    group = "Layout"

    async def execute(self, payload: DeleteCompLayoutPayload, user_id: Optional[str] = None) -> dict:
        start_t = time.monotonic()
        logger.info("[layouts/delete] start ids=%s user_id=%s", payload.ids, user_id)

        db = SessionLocal()
        try:
            rows: List[CompLayout] = db.query(CompLayout).filter(CompLayout.id.in_(payload.ids)).all()

            if not rows:
                logger.info("[layouts/delete] none found ids=%s", payload.ids)
                raise HTTPException(status_code=404, detail="No matching layout found")

            found_ids = {str(r.id) for r in rows}
            not_found = [i for i in payload.ids if i not in found_ids]

            blocked: List[dict] = []
            for r in rows:
                st = (getattr(r, "sub_type", None) or "").strip().lower()
                if st != "utility":
                    continue

                gid = getattr(r, "group_id", None)
                if not gid:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cannot delete utility layout '{r.id}': missing group_id",
                    )

                group_count = (
                    db.query(func.count(CompLayout.id))
                    .filter(CompLayout.group_id == gid)
                    .scalar()
                ) or 0

                if group_count > 1:
                    blocked.append(
                        {
                            "id": str(r.id),
                            "group_id": str(gid),
                            "count_in_group": int(group_count),
                        }
                    )

            if blocked:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Cannot delete: one or more layouts are still referenced by other records (same group_id).",
                        "blocked": blocked,
                    },
                )

            for r in rows:
                db.delete(r)
            db.commit()

            logger.info(
                "[layouts/delete] deleted_count=%d not_found=%d elapsed=%.3fs",
                len(found_ids),
                len(not_found),
                time.monotonic() - start_t,
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
            logger.exception("[layouts/delete] error")
            raise HTTPException(status_code=500, detail="Failed to delete layout")
        finally:
            db.close()
