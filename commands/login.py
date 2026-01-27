from commands.base_command import BaseCommand
from auth.db import SessionLocal
from auth.token import (
    create_access_token, create_refresh_token,
    REFRESH_TOKEN_EXPIRE_DAYS, TOKEN_EXPIRE_MINUTES
)
from passlib.context import CryptContext
from fastapi import HTTPException
from sqlalchemy import text
from datetime import datetime, timedelta
from pydantic import BaseModel, field_validator

# Support legacy bcrypt, prefer bcrypt_sha256 for new hashes
pwd_context = CryptContext(
    schemes=["bcrypt_sha256", "bcrypt"],
    deprecated="auto",
)

class LoginPayload(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Email must not be empty")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        # DO NOT strip() passwords — leading/trailing spaces are valid characters
        if v is None or v == "":
            raise ValueError("Password must not be empty")
        return v

class LoginCommand(BaseCommand):
    name = "login"
    schema = LoginPayload
    require_auth = False

    def run(self, payload: LoginPayload):
        db = SessionLocal()
        try:
            user = db.execute(
                text("SELECT id, password FROM tbl_users WHERE email = :email"),
                {"email": payload.email}
            ).fetchone()

            if not user:
                raise HTTPException(status_code=401, detail="Invalid credentials")

            # Verify against either bcrypt_sha256 or bcrypt (automatically)
            try:
                if not pwd_context.verify(payload.password, user.password):
                    raise HTTPException(status_code=401, detail="Invalid credentials")
            except ValueError:
                # Catches bcrypt's 72-byte error or malformed hashes
                raise HTTPException(status_code=401, detail="Invalid credentials")

            user_id = user.id
            access_token = create_access_token({"user_id": user_id})
            refresh_token = create_refresh_token({"user_id": user_id})

            expires_at = datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRE_MINUTES)
            refresh_token_exp = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

            db.execute(
                text("""
                    INSERT INTO tbl_tokens (
                        user_id, access_token, refresh_token, scope, expires_at, refresh_token_expires_at
                    )
                    VALUES (
                        :user_id, :access_token, :refresh_token, :scope, :expires_at, :refresh_token_expires_at
                    )
                """),
                {
                    "user_id": user_id,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "scope": "default",
                    "expires_at": expires_at,
                    "refresh_token_expires_at": refresh_token_exp
                }
            )
            db.commit()

            return {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "bearer",
                "expires_in": TOKEN_EXPIRE_MINUTES * 60,
            }
        finally:
            db.close()
