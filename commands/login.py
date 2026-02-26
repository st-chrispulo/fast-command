from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import HTTPException
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, field_validator

from auth.db import SessionLocal
from auth.token import REFRESH_TOKEN_EXPIRE_DAYS, TOKEN_EXPIRE_MINUTES, create_access_token, create_refresh_token
from commands.base_command import BaseCommand
from models.tbl_tokens import Token
from models.tbl_users import User

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.authentications.login")
except Exception:
    import logging

    logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt_sha256", "bcrypt"], deprecated="auto")


class LoginPayload(BaseModel):
    """Payload for user login."""

    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if v is None or v == "":
            raise ValueError("Password must not be empty")
        return v


class LoginCommand(BaseCommand):
    """Authenticates a user and issues access/refresh tokens."""

    name = "login"
    schema = LoginPayload
    require_auth = False

    def run(self, payload: LoginPayload):
        db = SessionLocal()
        try:
            user = (
                db.query(User.id, User.password)
                .filter(User.email == str(payload.email))
                .first()
            )

            if not user:
                raise HTTPException(status_code=401, detail="Invalid credentials")

            try:
                if not pwd_context.verify(payload.password, user.password):
                    raise HTTPException(status_code=401, detail="Invalid credentials")
            except ValueError:
                raise HTTPException(status_code=401, detail="Invalid credentials")

            user_id = int(user.id)
            access_token = create_access_token({"user_id": user_id})
            refresh_token = create_refresh_token({"user_id": user_id})

            now = datetime.utcnow()
            expires_at = now + timedelta(minutes=TOKEN_EXPIRE_MINUTES)
            refresh_token_exp = now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

            token_row = Token(
                user_id=user_id,
                access_token=access_token,
                refresh_token=refresh_token,
                scope="default",
                expires_at=expires_at,
                refresh_token_expires_at=refresh_token_exp,
            )

            db.add(token_row)
            db.commit()

            logger.info("Login successful", extra={"user_id": user_id, "scope": token_row.scope})

            return {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "bearer",
                "expires_in": TOKEN_EXPIRE_MINUTES * 60,
            }
        finally:
            db.close()
