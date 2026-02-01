# commands/components/navigations/delete.py

import time
from typing import Optional, List, Dict

from pydantic import BaseModel, field_validator
from fastapi import HTTPException
from sqlalchemy import func

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from models.components.tbl_comp_navigations import CompNavigation

# Prefer your app logger if available; fallback to stdlib
try:
    from logger import logger
except Exception:
    import logging as _logging
    logger = _logging.getLogger("delete_comp_navigation")
    if not logger.handlers:
        handler = _logging.StreamHandler()
        handler.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(_logging.DEBUG)


class DeleteCompNavigationPayload(BaseModel):
    # Accept a list of string IDs (avoid strict UUID parsing issues in Swagger/UI)
    ids: List[str]

    @field_validator("ids")
    @classmethod
    def _validate_ids(cls, v: List[str]) -> List[str]:
        if not isinstance(v, list):
            raise ValueError("ids must be a list")
        cleaned = [str(x).strip() for x in v if str(x or "").strip()]
        if not cleaned:
            raise ValueError("ids cannot be empty")
        seen, uniq = set(), []
        for x in cleaned:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq


class DeleteCompNavigationCommand(BaseCommand):
    """
    Hard-deletes one or more CompNavigation rows from the database.
    NOTE: Does NOT delete GCS objects. Handle storage cleanup separately.

    NEW RULE:
      - If sub_type == "utility":
          - must have group_id
          - block delete when there is more than 1 record with same group_id
            (meaning other records still reference that shared utility group)
    """

    name = "components/navigations/delete"
    schema = DeleteCompNavigationPayload
    require_auth = True
    method = "delete"
    type = "json"
    group = "Navigation"

    async def execute(self, payload: DeleteCompNavigationPayload, user_id: Optional[str] = None):
        start_t = time.monotonic()
        logger.info("[comp_navigations/delete] start ids=%s user_id=%s", payload.ids, user_id)

        db = SessionLocal()
        try:
            rows: List[CompNavigation] = (
                db.query(CompNavigation)
                .filter(CompNavigation.id.in_(payload.ids))
                .all()
            )

            if not rows:
                logger.info("[comp_navigations/delete] none found ids=%s", payload.ids)
                raise HTTPException(status_code=404, detail="No matching navigation found")

            found_ids = {str(r.id) for r in rows}
            not_found = [i for i in payload.ids if i not in found_ids]

            # ---- NEW: utility delete guard ----
            # If any row is utility, check its group_id usage count.
            # If count > 1 => other rows still reference it => block delete.
            #
            # NOTE: We check *current DB* count (not just 'rows' loaded),
            #       because user may delete only one of many.
            utility_blocks: List[Dict[str, str]] = []
            for r in rows:
                st = (getattr(r, "sub_type", None) or "").strip().lower()
                if st != "utility":
                    continue

                gid = getattr(r, "group_id", None)
                if not gid:
                    # Be strict: utility without group_id means we can't reason about references safely
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
                        {
                            "id": str(r.id),
                            "group_id": str(gid),
                            "count_in_group": str(group_count),
                        }
                    )

            if utility_blocks:
                # Return a clear error message. You can also include which IDs were blocked.
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Cannot delete: one or more navigations are still referenced by other records (same group_id).",
                        "blocked": utility_blocks,
                    },
                )

            # ---- delete ----
            for r in rows:
                db.delete(r)
            db.commit()

            elapsed = time.monotonic() - start_t
            logger.info(
                "[comp_navigations/delete] deleted_count=%d not_found=%d elapsed=%.3fs",
                len(found_ids), len(not_found), elapsed
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
            raise HTTPException(status_code=500, detail="Failed to delete navigation")
        finally:
            db.close()
