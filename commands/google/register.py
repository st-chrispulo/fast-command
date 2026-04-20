import json
import secrets
import string
from datetime import datetime, timedelta
from typing import Optional

from pydantic import BaseModel, field_validator
from fastapi import HTTPException, Request
from sqlalchemy import text
from passlib.context import CryptContext

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from auth.token import (
    create_access_token,
    create_refresh_token,
    verify_token,
    TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_DAYS,
)

try:
    from logger import logger as _app_logger
    logger = _app_logger.getChild("commands.google.register")
except Exception:
    import logging
    logger = logging.getLogger(__name__)


pwd_context = CryptContext(schemes=["bcrypt_sha256", "bcrypt"], deprecated="auto")


def _generate_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


class RegisterGoogleUserPayload(BaseModel):
    temp_token: str

    @field_validator("temp_token", mode="before")
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v


class RegisterGoogleUserCommand(BaseCommand):
    name = "google/user/register"
    schema = RegisterGoogleUserPayload
    require_auth = False

    def run(self, payload: RegisterGoogleUserPayload, request: Optional[Request] = None):
        token_data = verify_token(payload.temp_token)

        if token_data.get("type") != "google_register":
            raise HTTPException(status_code=400, detail="Invalid token type.")

        google_user_id = token_data.get("sub")
        email = token_data.get("email")
        name = token_data.get("name")
        picture = token_data.get("picture")

        db = SessionLocal()
        try:
            user_row = db.execute(
                text("SELECT id FROM tbl_users WHERE email = :email"),
                {"email": email},
            ).fetchone()

            if not user_row:
                username = email.split("@")[0].lower()
                random_pw = _generate_password()
                hashed_pw = pwd_context.hash(random_pw)

                user_row = db.execute(
                    text(
                        """
                        INSERT INTO tbl_users (username, email, password)
                        VALUES (:username, :email, :password)
                        RETURNING id
                        """
                    ),
                    {
                        "username": username,
                        "email": email,
                        "password": hashed_pw,
                    },
                ).fetchone()

            user_id = user_row[0]

            profile_json = {
                "sub": google_user_id,
                "email": email,
                "name": name,
                "picture": picture,
            }

            db.execute(
                text(
                    """
                    INSERT INTO tbl_user_google (
                        user_id, google_user_id, email, name, picture,
                        access_token_enc, token_type, token_scope,
                        expires_at, profile_json, last_synced_at
                    )
                    VALUES (
                        :user_id, :google_user_id, :email, :name, :picture,
                        NULL, NULL, NULL,
                        NULL, CAST(:profile_json AS JSONB), NOW()
                    )
                    ON CONFLICT (google_user_id) DO UPDATE SET
                        user_id = EXCLUDED.user_id,
                        email = EXCLUDED.email,
                        name = EXCLUDED.name,
                        picture = EXCLUDED.picture,
                        profile_json = EXCLUDED.profile_json,
                        last_synced_at = NOW(),
                        updated_at = NOW()
                    """
                ),
                {
                    "user_id": user_id,
                    "google_user_id": google_user_id,
                    "email": email,
                    "name": name,
                    "picture": picture,
                    "profile_json": json.dumps(profile_json),
                },
            )
            access_token = create_access_token({"user_id": user_id})
            refresh_token = create_refresh_token({"user_id": user_id})

            db.commit()

            return {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "bearer",
                "expires_in": TOKEN_EXPIRE_MINUTES * 60,
                "user_id": user_id,
                "email": email,
            }

        finally:
            db.close()