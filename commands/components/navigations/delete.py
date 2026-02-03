from __future__ import annotations

import time
from typing import Dict, List, Optional

from fastapi import HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import func

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.components.tbl_comp_navigations import CompNavigation

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.navigations.delete")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class DeleteCompNavigationPayload(BaseModel):
    ids: List[str]

    @field_validator("ids")
    @classmethod
    def validate_ids(cls, v: List[str]) -> List[str]:
        if not isinstance(v, list):
            raise ValueError("ids must be a list")
        cleaned = [str(x).strip() for x in v if str(x or "").strip()]
        if not cleaned:
            raise ValueError("ids cannot be empty")
        seen: set[str] = set()
        uniq: List[str] = []
        for x in cleaned:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq


class DeleteCompNavigationCommand(BaseCommand):
    """Hard-delete one or more CompNavigation rows.

    Utility rule:
        When sub_type == "utility", group_id must be present and deletion is blocked when
        more than one record exists with the same group_id.
    """

    name = "components/navigations/delete"
    schema = DeleteCompNavigationPayload
    require_auth = True
    method = "delete"
    type = "json"
    group = "Navigation"

    async def execute(self, payload: DeleteCompNavigationPayload, user_id: Optional[str] = None) -> Dict[str, object]:
        start_t = time.monotonic()
        logger.info("[comp_navigations/delete] start ids=%s user_id=%s", payload.ids, user_id)

        db = SessionLocal()
        try:
            rows: List[CompNavigation] = (
                db.query(CompNavigation).filter(CompNavigation.id.in_(payload.ids)).all()
            )
            if not rows:
                logger.info("[comp_navigations/delete] none found ids=%s", payload.ids)
                raise HTTPException(status_code=404, detail="No matching navigation found")

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
                        detail=f"Cannot delete utility navigation '{r.id}': missing group_id",
                    )

                group_count = (
                    db.query(func.count(CompNavigation.id))
                    .filter(CompNavigation.group_id == gid)
                    .scalar()
                ) or 0

                if group_count > 1:
                    utility_blocks.append(
                        {"id": str(r.id), "group_id": str(gid), "count_in_group": str(group_count)}
                    )

            if utility_blocks:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Cannot delete: one or more navigations are still referenced by other records (same group_id).",
                        "blocked": utility_blocks,
                    },
                )

            for r in rows:
                db.delete(r)
            db.commit()

            elapsed = time.monotonic() - start_t
            logger.info(
                "[comp_navigations/delete] deleted_count=%d not_found=%d elapsed=%.3fs",
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
            logger.exception("[comp_navigations/delete] error - rolling back: %s", e)
            db.rollback()
            raise HTTPException(status_code=500, detail="Failed to delete navigation") from e
        finally:
            db.close()
