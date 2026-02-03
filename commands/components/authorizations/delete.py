from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Set
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import func

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_authentications import CompAuthentication

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.authentications.delete")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class DeleteCompAuthenticationsPayload(BaseModel):
    """Delete one or many authentication components."""

    id: Optional[UUID] = None
    ids: Optional[List[UUID]] = None
    hard_delete_files: bool = True
    ignore_missing: bool = True

    @field_validator("ids", mode="before")
    @classmethod
    def cleanup_ids(cls, v: Any) -> Any:
        if v is None:
            return v
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    @model_validator(mode="after")
    def xor_ids(self) -> "DeleteCompAuthenticationsPayload":
        has_id = self.id is not None
        has_ids = bool(self.ids)
        if has_id == has_ids:
            raise ValueError("Provide either 'id' or 'ids' (exclusively).")
        return self


def _collect_all_keys(row: CompAuthentication) -> Set[str]:
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
    """Delete authentication component(s)."""

    name = "components/authentications/delete"
    schema = DeleteCompAuthenticationsPayload
    require_auth = True
    method = "delete"
    type = "action"
    group = "Authentication"

    async def execute(self, payload: DeleteCompAuthenticationsPayload, user_id: Optional[str] = None) -> dict:
        t0 = time.monotonic()

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        ids: List[str] = [str(payload.id)] if payload.id else [str(x) for x in (payload.ids or [])]

        db = SessionLocal()
        gcs = get_gcs()

        files_to_delete: Set[str] = set()
        deleted_ids: List[str] = []
        not_found_ids: List[str] = []
        errors: List[Dict[str, Any]] = []

        logger.info(
            "authentications.delete.start user_id=%s ids=%s hard_delete_files=%s ignore_missing=%s",
            user_id,
            len(ids),
            payload.hard_delete_files,
            payload.ignore_missing,
        )

        try:
            rows: List[CompAuthentication] = (
                db.query(CompAuthentication).filter(CompAuthentication.id.in_(ids)).all()
            )

            found_map = {str(r.id): r for r in rows}
            for _id in ids:
                if _id not in found_map:
                    not_found_ids.append(_id)

            if not payload.ignore_missing and not_found_ids:
                raise HTTPException(status_code=404, detail=f"IDs not found: {', '.join(not_found_ids)}")

            utility_group_ids: Set[str] = set()
            for r in rows:
                gid = getattr(r, "group_id", None)
                if _is_utility_sub_type(r) and gid:
                    utility_group_ids.add(str(gid))

            if utility_group_ids:
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

                    if total_count > deleting_count:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"Cannot delete: group_id {gid} is still used by other components "
                                f"({total_count - deleting_count} remaining)."
                            ),
                        )

            if payload.hard_delete_files:
                for r in rows:
                    files_to_delete |= _collect_all_keys(r)

            for r in rows:
                try:
                    db.delete(r)
                    deleted_ids.append(str(r.id))
                except Exception as e:
                    logger.exception("authentications.delete.row_failed id=%s", str(r.id))
                    errors.append({"id": str(r.id), "error": str(e)})

            db.commit()

            files_deleted = 0
            if payload.hard_delete_files and files_to_delete:
                for key in files_to_delete:
                    try:
                        gcs.delete_object(key)
                        files_deleted += 1
                    except Exception:
                        logger.warning("authentications.delete.gcs_delete_failed key=%s", key)

            perf_ms = round((time.monotonic() - t0) * 1000, 2)
            logger.info(
                "authentications.delete.success user_id=%s deleted=%s not_found=%s files_deleted=%s errors=%s perf_ms=%s",
                user_id,
                len(deleted_ids),
                len(not_found_ids),
                files_deleted,
                len(errors),
                perf_ms,
            )

            return {
                "status": "ok",
                "deleted_ids": deleted_ids,
                "not_found_ids": not_found_ids,
                "files_deleted": files_deleted,
                "errors": errors,
                "perf_ms": perf_ms,
            }

        except HTTPException:
            db.rollback()
            logger.exception(
                "authentications.delete.http_error user_id=%s deleted=%s not_found=%s",
                user_id,
                len(deleted_ids),
                len(not_found_ids),
            )
            raise
        except Exception:
            db.rollback()
            logger.exception("authentications.delete.db_error user_id=%s", user_id)
            raise
        finally:
            db.close()
