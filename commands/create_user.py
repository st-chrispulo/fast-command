from __future__ import annotations

from fastapi import HTTPException
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.tbl_users import User

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.authentications.create_user")
except Exception:
    import logging

    logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt_sha256", "bcrypt"], deprecated="auto")


class CreateUserPayload(BaseModel):
    """Payload for creating a user."""

    email: EmailStr
    username: str = Field(min_length=3, max_length=20)
    password: str = Field(min_length=6)

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Username must not be empty")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if not v:
            raise ValueError("Password must not be empty")
        return v


class CreateUserCommand(BaseCommand):
    """Creates a new user record."""

    name = "create_user"
    schema = CreateUserPayload
    require_auth = False

    def run(self, payload: CreateUserPayload):
        db = SessionLocal()
        try:
            exists = db.query(User.id).filter(User.email == str(payload.email)).first()
            if exists:
                raise HTTPException(status_code=400, detail="Email already registered")

            user = User(
                email=str(payload.email),
                username=payload.username,
                password=pwd_context.hash(payload.password),
            )

            db.add(user)
            db.commit()

            logger.info("User created", extra={"user_id": user.id, "email": user.email})
            return {"status": "User created successfully"}
        finally:
            db.close()
