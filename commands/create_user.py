from commands.base_command import BaseCommand
from auth.db import SessionLocal
from passlib.hash import bcrypt
from fastapi import HTTPException
from sqlalchemy import text
from pydantic import BaseModel, EmailStr, field_validator


class CreateUserPayload(BaseModel):
    email: str
    username: str
    password: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Email must not be empty")
        try:
            EmailStr.validate(v)
        except Exception:
            raise ValueError("Invalid email format")
        return v

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Username must not be empty")
        if len(v) < 3:
            raise ValueError("Username must be at least 3 characters")
        if len(v) > 20:
            raise ValueError("Username must not exceed 20 characters")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Password must not be empty")
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters")
        return v


class CreateUserCommand(BaseCommand):
    name = "create_user"
    schema = CreateUserPayload
    require_auth = False

    def run(self, payload: CreateUserPayload):
        db = SessionLocal()
        try:
            existing = db.execute(
                text("SELECT 1 FROM tbl_users WHERE email = :email"),
                {"email": payload.email}
            ).fetchone()

            if existing:
                raise HTTPException(status_code=400, detail="Email already registered")

            hashed_pw = bcrypt.hash(payload.password)

            db.execute(
                text("""
                    INSERT INTO tbl_users (email, username, password)
                    VALUES (:email, :username, :password)
                """),
                {
                    "email": payload.email,
                    "username": payload.username,
                    "password": hashed_pw
                }
            )
            db.commit()
            return {"status": "User created successfully"}
        finally:
            db.close()

