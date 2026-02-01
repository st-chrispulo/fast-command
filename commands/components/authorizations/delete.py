# commands/components/authentications/delete.py

import time
from typing import List, Optional, Dict, Any, Set
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import func

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_authentications import CompAuthentication

# Prefer your app logger if available; fallback to stdlib
try:
    from logger import logger
except Exception:
    import logging as _logging
    logger = _logging.getLogger("authentications_delete")
    if not logger.handlers:
        handler = _logging.StreamHandler()
        handler.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(_logging.DEBUG)


class DeleteCompAuthenticationsPayload(BaseModel):
    """Delete one or many authentication components.
    You must provide either `id` or `ids` (not both).
    """
    id: Optional[UUID] = None
    ids: Optional[List[UUID]] = None

    # Optional behavior flags
    hard_delete_files: bool = True  # also delete GCS objects (thumbnail/images/file_link)
    ignore_missing: bool = True     # skip unknown IDs instead of 404

    @field_validator("ids", mode="before")
    @classmethod
    def _cleanup_ids(cls, v):
        if v is None:
            return v
        # Allow comma-separated strings or arrays
        if isinstance(v, str):
            parts = [p.strip() for p in v.split(",") if p.strip()]
            return parts
        return v

    @model_validator(mode="after")
    def _xor_ids(self):
        has_id = self.id is not None
        has_ids = bool(self.ids)
        if has_id == has_ids:
            raise ValueError("Provide either 'id' or 'ids' (exclusively).")
        return self


def _collect_all_keys(row: CompAuthentication) -> Set[str]:
    """
    Updated model:
      - thumbnail (TEXT)
      - images (JSONB array of keys)
      - file_link (TEXT, single key)
    """
    keys: Set[str] = set()

    thumb = getattr(row, "thumbnail", None)
    if thumb:
        keys.add(thumb)

    for k in getattr(row, "images", None) or []:
        if k:
            keys.add(k)

    file_key = getattr(row, "file_link", None)
    if file_key:
        keys.add(file_key)

    return keys


def _is_utility_sub_type(row: CompAuthentication) -> bool:
    return (getattr(row, "sub_type", None) or "").strip().lower() == "utility"


class DeleteCompAuthenticationsCommand(BaseCommand):
    """
    Delete authentication component(s).

    Catch rule:
      - If sub_type == "utility" AND group_id is shared by other rows,
        block deletion and return an error saying there are still other
        components using it.
    """
    name = "components/authentications/delete"
    schema = DeleteCompAuthenticationsPayload
    require_auth = True
    method = "delete"
    type = "action"
    group = "Authentication"

    async def execute(
        self,
        payload: DeleteCompAuthenticationsPayload,
        user_id: Optional[str] = None,
    ):
        t0 = time.monotonic()
        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        # Normalize IDs list
        ids: List[str] = [str(payload.id)] if payload.id else [str(x) for x in (payload.ids or [])]

        db = SessionLocal()
        gcs = get_gcs()

        files_to_delete: Set[str] = set()
        deleted_ids: List[str] = []
        not_found_ids: List[str] = []
        errors: List[Dict[str, Any]] = []

        try:
            # Load rows
            rows: List[CompAuthentication] = (
                db.query(CompAuthentication)
                .filter(CompAuthentication.id.in_(ids))
                .all()
            )

            found_map = {str(r.id): r for r in rows}
            for _id in ids:
                if _id not in found_map:
                    not_found_ids.append(_id)

            if not payload.ignore_missing and not_found_ids:
                raise HTTPException(status_code=404, detail=f"IDs not found: {', '.join(not_found_ids)}")

            # ---- Catch rule check (utility + shared group_id) ----
            # We do this BEFORE deleting anything.
            utility_group_ids: Set[str] = set()
            for r in rows:
                if _is_utility_sub_type(r) and getattr(r, "group_id", None):
                    utility_group_ids.add(str(r.group_id))

            if utility_group_ids:
                # Count occurrences of these group_ids in this table.
                # If count > number of rows we are deleting for that group_id,
                # it means other components still reference it.
                for gid in utility_group_ids:
                    total_count = (
                        db.query(func.count(CompAuthentication.id))
                        .filter(CompAuthentication.group_id == gid)
                        .scalar()
                        or 0
                    )

                    deleting_count = sum(
                        1
                        for r in rows
                        if getattr(r, "group_id", None) is not None and str(r.group_id) == gid
                    )

                    # If there will still be remaining rows with same group_id after this delete
                    if total_count > deleting_count:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"Cannot delete: group_id {gid} is still used by other components "
                                f"({total_count - deleting_count} remaining)."
                            ),
                        )

            # Collect files to delete (before DB delete)
            if payload.hard_delete_files:
                for r in rows:
                    files_to_delete |= _collect_all_keys(r)

            # Delete rows
            for r in rows:
                try:
                    db.delete(r)
                    deleted_ids.append(str(r.id))
                except Exception as e:
                    logger.exception("[authentications:delete] failed for id=%s", str(r.id))
                    errors.append({"id": str(r.id), "error": str(e)})

            db.commit()

            # Best-effort file deletions (ignore individual failures)
            files_deleted = 0
            if payload.hard_delete_files and files_to_delete:
                for key in files_to_delete:
                    try:
                        gcs.delete_object(key)
                        files_deleted += 1
                    except Exception:
                        logger.warning("[authentications:delete] couldn't delete GCS key=%s", key)

            return {
                "status": "ok",
                "deleted_ids": deleted_ids,
                "not_found_ids": not_found_ids,
                "files_deleted": files_deleted,
                "errors": errors,
                "perf_ms": round((time.monotonic() - t0) * 1000, 2),
            }

        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            logger.exception("[authentications:delete] DB error")
            raise
        finally:
            db.close()
