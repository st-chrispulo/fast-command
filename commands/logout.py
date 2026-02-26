from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.tbl_tokens import Token

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.authentications.logout")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class LogoutPayload(BaseModel):
    """Payload for logging out a user."""

    user_id: int

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("user_id must be a positive integer")
        return v


class LogoutCommand(BaseCommand):
    """Revokes all tokens for a user."""

    name = "logout"
    schema = LogoutPayload
    require_auth = False

    def run(self, payload: LogoutPayload):
        db = SessionLocal()
        try:
            updated = (
                db.query(Token)
                .filter(Token.user_id == payload.user_id, Token.revoked.is_(False))
                .update({"revoked": True}, synchronize_session=False)
            )
            db.commit()

            if updated == 0:
                raise HTTPException(status_code=404, detail="No tokens found for user")

            logger.info("Logout successful", extra={"user_id": payload.user_id, "revoked_count": updated})
            return {"message": "Logged out successfully"}
        finally:
            db.close()
