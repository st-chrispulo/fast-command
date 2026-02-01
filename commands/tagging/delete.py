# commands/tagging/delete.py
import time
from typing import List, Optional
from fastapi import HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import select, delete, update

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from models.tbl_user_tags import UserTag

# Prefer your app logger if available; fallback to stdlib
try:
    from logger import logger
except Exception:
    import logging as _logging
    logger = _logging.getLogger("tagging_delete")
    if not logger.handlers:
        handler = _logging.StreamHandler()
        handler.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(_logging.DEBUG)


class DeleteTagPayload(BaseModel):
    """
    Delete tags for the authenticated user.

    Either provide:
      - name: str           # single tag name
    or
      - names: List[str]    # multiple tag names

    soft: if True, performs a soft delete (is_active = FALSE) instead of hard delete.
    """
    name: Optional[str] = None
    names: Optional[List[str]] = None
    soft: bool = False

    @field_validator("names", mode="before")
    @classmethod
    def _normalize_names(cls, v):
        if v is None:
            return None
        if isinstance(v, (list, tuple, set)):
            items = [str(x).strip() for x in v if str(x).strip()]
            return list(dict.fromkeys(items))  # de-dup, preserve order
        return None

    @field_validator("name")
    @classmethod
    def _trim_name(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("*")
    @classmethod
    def _at_least_one(cls, v, values):
        # After individual validators run, ensure at least one name exists
        name = values.get("name")
        names = values.get("names")
        if (not name or not str(name).strip()) and (not names or len(names) == 0):
            raise ValueError("Provide 'name' or 'names'.")
        return v

    def all_names(self) -> List[str]:
        if self.names and len(self.names) > 0:
            return self.names
        if self.name:
            return [self.name]
        return []


class TagDeleteCommand(BaseCommand):
    """
    Delete tags in the authenticated user's namespace.
    - Hard delete (default): removes rows.
    - Soft delete (soft=True): sets is_active = FALSE.
    """
    name = "tagging/delete"
    schema = DeleteTagPayload
    require_auth = True
    method = "delete"
    group = "Tagging"

    async def execute(self, payload: DeleteTagPayload, user_id: Optional[int] = None):
        t0 = time.monotonic()
        names = payload.all_names()

        if self.require_auth and user_id is None:
            logger.warning("[tagging/delete] unauthorized - no user context")
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        db = SessionLocal()
        try:
            # Determine which exist (for nice reporting)
            existing = db.execute(
                select(UserTag.name).where(
                    UserTag.user_id == user_id,
                    UserTag.name.in_(names),
                )
            ).scalars().all()
            existing_set = set(existing)
            not_found = [n for n in names if n not in existing_set]

            if payload.soft:
                # Soft delete -> set is_active = FALSE for existing names
                res = db.execute(
                    update(UserTag)
                    .where(UserTag.user_id == user_id, UserTag.name.in_(existing))
                    .values(is_active=False)
                )
                affected = res.rowcount or 0
                action = "soft_deleted"
            else:
                # Hard delete -> remove rows
                res = db.execute(
                    delete(UserTag)
                    .where(UserTag.user_id == user_id, UserTag.name.in_(existing))
                )
                affected = res.rowcount or 0
                action = "hard_deleted"

            db.commit()
            elapsed = time.monotonic() - t0
            logger.info(
                "[tagging/delete] %s=%d not_found=%d elapsed=%.3fs user_id=%s",
                action, affected, len(not_found), elapsed, user_id
            )

            return {
                "status": "ok",
                "data": {
                    "user_id": user_id,
                    action: sorted(list(existing_set)),
                    "not_found": not_found,
                    "count": affected,
                    "soft": payload.soft,
                },
            }

        except SQLAlchemyError as e:
            logger.exception("[tagging/delete] DB error - rolling back: %s", e)
            db.rollback()
            raise HTTPException(status_code=500, detail="Database error")
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("[tagging/delete] unexpected error - rolling back: %s", e)
            db.rollback()
            raise HTTPException(status_code=500, detail="Internal server error")
        finally:
            db.close()


__all__ = ["TagDeleteCommand"]
