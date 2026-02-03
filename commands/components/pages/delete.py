from __future__ import annotations

import time
from typing import Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import func

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.components.tbl_comp_pages import CompPage

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.pages.delete")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class DeleteCompPagePayload(BaseModel):
    ids: List[str]

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


def parse_uuid_ids(ids: List[str]) -> List[UUID]:
    out: List[UUID] = []
    bad: List[str] = []
    for s in ids:
        try:
            out.append(UUID(str(s)))
        except Exception:
            bad.append(str(s))
    if bad:
        raise HTTPException(status_code=400, detail={"message": "Invalid UUID in ids", "invalid_ids": bad})
    return out


class DeleteCompPageCommand(BaseCommand):
    name = "components/pages/delete"
    schema = DeleteCompPagePayload
    require_auth = True
    method = "delete"
    type = "json"
    group = "Page"

    async def execute(self, payload: DeleteCompPagePayload, user_id: Optional[str] = None) -> dict:
        start_t = time.monotonic()
        logger.info("[comp_pages/delete] start ids=%s user_id=%s", payload.ids, user_id)

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        uuids = parse_uuid_ids(payload.ids)

        db = SessionLocal()
        try:
            rows: List[CompPage] = db.query(CompPage).filter(CompPage.id.in_(uuids)).all()
            if not rows:
                logger.info("[comp_pages/delete] none found ids=%s", payload.ids)
                raise HTTPException(status_code=404, detail="No matching page found")

            found_ids = {str(r.id) for r in rows}
            not_found = [i for i in payload.ids if i not in found_ids]

            utility_blocks: List[Dict[str, str]] = []
            for r in rows:
                st = (getattr(r, "sub_type", None) or "").strip().lower()
                if st != "utility":
                    continue

                gid = getattr(r, "group_id", None)
                if not gid:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cannot delete utility page '{r.id}': missing group_id",
                    )

                group_count = (
                    db.query(func.count(CompPage.id))
                    .filter(CompPage.group_id == gid)
                    .scalar()
                ) or 0

                if group_count > 1:
                    utility_blocks.append(
                        {
                            "id": str(r.id),
                            "group_id": str(gid),
                            "count_in_group": str(group_count),
                        }
                    )

            if utility_blocks:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Cannot delete: one or more pages are still referenced by other records (same group_id).",
                        "blocked": utility_blocks,
                    },
                )

            for r in rows:
                db.delete(r)
            db.commit()

            elapsed = time.monotonic() - start_t
            logger.info(
                "[comp_pages/delete] deleted_count=%d not_found=%d elapsed=%.3fs",
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
            logger.exception("[comp_pages/delete] error - rolling back: %s", e)
            db.rollback()
            raise HTTPException(status_code=500, detail="Failed to delete page") from e
        finally:
            db.close()
