from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import HTTPException
from pydantic import BaseModel, field_validator

from auth.db import SessionLocal
from auth.token import (
    REFRESH_TOKEN_EXPIRE_DAYS,
    TOKEN_EXPIRE_MINUTES,
    create_access_token,
    create_refresh_token,
    get_token_payload,
)
from commands.base_command import BaseCommand
from models.tbl_tokens import Token

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.authentications.refresh_token")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class RefreshTokenPayload(BaseModel):
    """Payload for refreshing tokens."""

    refresh_token: str

    @field_validator("refresh_token")
    @classmethod
    def validate_refresh_token(cls, v: str) -> str:
        if v is None or v.strip() == "":
            raise ValueError("refresh_token must not be empty")
        return v


class RefreshTokenCommand(BaseCommand):
    """Rotates refresh token and issues a new access token."""

    name = "refresh_token"
    schema = RefreshTokenPayload
    require_auth = False

    def run(self, payload: RefreshTokenPayload):
        db = SessionLocal()
        try:
            token_data = get_token_payload(payload.refresh_token)
            user_id = token_data.get("user_id")
            if not user_id:
                raise HTTPException(status_code=401, detail="Invalid refresh token")

            now = datetime.utcnow()

            token_row = (
                db.query(Token)
                .filter(
                    Token.user_id == int(user_id),
                    Token.refresh_token == payload.refresh_token,
                    Token.revoked.is_(False),
                    Token.refresh_token_expires_at > now,
                )
                .first()
            )

            if not token_row:
                raise HTTPException(status_code=401, detail="Refresh token is invalid or expired")

            new_access_token = create_access_token({"user_id": int(user_id)})
            new_refresh_token = create_refresh_token({"user_id": int(user_id)})

            access_expires_at = now + timedelta(minutes=TOKEN_EXPIRE_MINUTES)
            refresh_expires_at = now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

            token_row.access_token = new_access_token
            token_row.refresh_token = new_refresh_token
            token_row.expires_at = access_expires_at
            token_row.refresh_token_expires_at = refresh_expires_at

            db.commit()

            logger.info("Refresh token rotated", extra={"user_id": int(user_id), "token_id": token_row.id})

            return {
                "access_token": new_access_token,
                "refresh_token": new_refresh_token,
                "token_type": "bearer",
                "expires_in": TOKEN_EXPIRE_MINUTES * 60,
            }
        finally:
            db.close()
