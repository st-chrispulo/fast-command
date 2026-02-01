# commands/tagging/create.py
import re
import time
from typing import Optional
from fastapi import HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.exc import SQLAlchemyError

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from models.tbl_user_tags import UserTag

# Prefer your app logger if available; fallback to stdlib
try:
    from logger import logger
except Exception:
    import logging as _logging
    logger = _logging.getLogger("tagging_create")
    if not logger.handlers:
        handler = _logging.StreamHandler()
        handler.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(_logging.DEBUG)

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


class CreateTagPayload(BaseModel):
    """
    Create (or update) a tag for the authenticated user.
    Composite key = (user_id, name)
    """
    name: str
    color_hex: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = True

    @field_validator("name")
    @classmethod
    def _name_required(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("name is required")
        # Keep original casing; PK is case-sensitive. If you want CI, adjust model/index.
        return v

    @field_validator("color_hex", mode="before")
    @classmethod
    def _normalize_color(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        if not v:
            return None
        if not _HEX_RE.match(v):
            raise ValueError("color_hex must be in '#RRGGBB' format")
        return v

    @field_validator("description", mode="before")
    @classmethod
    def _strip_desc(cls, v):
        return v.strip() if isinstance(v, str) else v


def _serialize_tag(m: UserTag) -> dict:
    return {
        "user_id": m.user_id,
        "name": m.name,
        "color_hex": m.color_hex,
        "description": m.description,
        "is_active": bool(m.is_active),
        "created_at": m.created_at.isoformat() if getattr(m, "created_at", None) else None,
        "updated_at": m.updated_at.isoformat() if getattr(m, "updated_at", None) else None,
    }


class TagCreateCommand(BaseCommand):
    """
    Create or update a tag in the authenticated user's namespace.
    If the (user_id, name) exists, this acts like an update (idempotent upsert).
    """
    name = "tagging/create"
    schema = CreateTagPayload
    require_auth = True
    method = "post"
    group = "Tagging"

    async def execute(self, payload: CreateTagPayload, user_id: Optional[int] = None):
        t0 = time.monotonic()
        logger.info("[tagging/create] start user_id=%s name=%s", user_id, payload.name)

        if self.require_auth and user_id is None:
            logger.warning("[tagging/create] unauthorized - no user context")
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        db = SessionLocal()
        try:
            # Look up existing tag by composite PK (user_id, name)
            tag = db.get(UserTag, (user_id, payload.name))

            if tag:
                # Update path
                logger.debug("[tagging/create] update existing tag")
                if payload.color_hex is not None:
                    tag.color_hex = payload.color_hex
                if payload.description is not None:
                    tag.description = payload.description
                if payload.is_active is not None:
                    tag.is_active = payload.is_active
                # updated_at is handled by onupdate=func.now()
            else:
                # Create path
                logger.debug("[tagging/create] create new tag")
                tag = UserTag(
                    user_id=user_id,
                    name=payload.name,
                    color_hex=payload.color_hex,
                    description=payload.description,
                    is_active=True if payload.is_active is None else payload.is_active,
                )
                db.add(tag)

            db.commit()
            db.refresh(tag)
            elapsed = time.monotonic() - t0
            logger.info("[tagging/create] ok elapsed=%.3fs user_id=%s name=%s", elapsed, user_id, payload.name)

            return {
                "status": "ok",
                "data": _serialize_tag(tag),
            }

        except SQLAlchemyError as e:
            logger.exception("[tagging/create] DB error - rolling back: %s", e)
            db.rollback()
            raise HTTPException(status_code=500, detail="Database error")
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("[tagging/create] unexpected error - rolling back: %s", e)
            db.rollback()
            raise HTTPException(status_code=500, detail="Internal server error")
        finally:
            db.close()
