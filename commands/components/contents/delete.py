from __future__ import annotations

import time
from typing import List, Optional, Dict, Any, Set

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from models.components.tbl_comp_contents import CompContent

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.contents.delete")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class DeleteCompContentPayload(BaseModel):
    ids: List[str] = Field(description="List of content IDs to hard-delete")

    @field_validator("ids")
    @classmethod
    def validate_ids(cls, v: List[str]) -> List[str]:
        if not isinstance(v, list):
            raise ValueError("ids must be a list")
        cleaned = [str(x).strip() for x in v if str(x or "").strip()]
        if not cleaned:
            raise ValueError("ids cannot be empty")

        seen: Set[str] = set()
        uniq: List[str] = []
        for x in cleaned:
            if x in seen:
                continue
            seen.add(x)
            uniq.append(x)
        return uniq


class DeleteCompContentCommand(BaseCommand):
    name = "components/contents/delete"
    schema = DeleteCompContentPayload
    require_auth = True
    method = "delete"
    type = "json"
    group = "Content"

    async def execute(self, payload: DeleteCompContentPayload, user_id: Optional[str] = None) -> Dict[str, Any]:
        start_t = time.monotonic()
        logger.info("[components.contents.delete] start ids=%s user_id=%s", payload.ids, user_id)

        db = SessionLocal()
        try:
            rows: List[CompContent] = (
                db.query(CompContent)
                .filter(CompContent.id.in_(payload.ids))
                .all()
            )

            if not rows:
                raise HTTPException(status_code=404, detail="No matching content found")

            found_ids = {str(r.id) for r in rows}
            not_found = [i for i in payload.ids if i not in found_ids]

            blocked = self._validate_utility_delete_guard(db, rows)
            if blocked:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Cannot delete: one or more contents are still referenced by other records (same group_id).",
                        "blocked": blocked,
                    },
                )

            for r in rows:
                db.delete(r)

            db.commit()

            elapsed = time.monotonic() - start_t
            logger.info(
                "[components.contents.delete] deleted_count=%d not_found=%d elapsed=%.3fs",
                len(found_ids),
                len(not_found),
                elapsed,
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
        except Exception as e:
            db.rollback()
            logger.exception("[components.contents.delete] error=%s", e)
            raise HTTPException(status_code=500, detail="Failed to delete content")
        finally:
            db.close()

    @staticmethod
    def _validate_utility_delete_guard(db: SessionLocal, rows: List[CompContent]) -> List[Dict[str, str]]:
        blocked: List[Dict[str, str]] = []

        for r in rows:
            sub_type = (getattr(r, "sub_type", None) or "").strip().lower()
            if sub_type != "utility":
                continue

            gid = getattr(r, "group_id", None)
            if not gid:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot delete utility content '{r.id}': missing group_id",
                )

            group_count = (
                db.query(func.count(CompContent.id))
                .filter(CompContent.group_id == gid)
                .scalar()
            ) or 0

            if group_count > 1:
                blocked.append(
                    {
                        "id": str(r.id),
                        "group_id": str(gid),
                        "count_in_group": str(group_count),
                    }
                )

        return blocked
