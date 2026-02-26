from __future__ import annotations

from typing import List

from fastapi import HTTPException
from pydantic import BaseModel, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.tbl_roles import Role
from models.tbl_user_permissions import UserPermission
from models.tbl_users import User

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.roles.assign")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class AssignRolePayload(BaseModel):
    """Payload for assigning a role to a user."""

    user: str
    role_name: str

    @field_validator("user")
    @classmethod
    def validate_user(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("User must not be empty")
        return v

    @field_validator("role_name")
    @classmethod
    def validate_role_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Role name must not be empty")
        return v


class AssignRoleToUserCommand(BaseCommand):
    """Assigns a role's command permissions to a user."""

    name = "role/assign"
    schema = AssignRolePayload

    def run(self, payload: AssignRolePayload):
        db = SessionLocal()
        try:
            user = (
                db.query(User)
                .filter(User.username == payload.user)
                .first()
            )

            if not user:
                user = (
                    db.query(User)
                    .filter(User.email == payload.user)
                    .first()
                )

            if not user:
                raise HTTPException(status_code=404, detail=f"User '{payload.user}' not found.")

            role = (
                db.query(Role)
                .filter(Role.name == payload.role_name)
                .first()
            )

            if not role:
                raise HTTPException(status_code=404, detail=f"Role '{payload.role_name}' not found.")

            commands: List[str] = role.command_names or []
            if not commands:
                logger.info(
                    "Role has no commands",
                    extra={"user_id": user.id, "role_id": role.id, "role_name": role.name},
                )
                return {"status": f"Role '{payload.role_name}' assigned to user '{payload.user}'"}

            rows = [
                UserPermission(user_id=user.id, command_name=cmd, granted_by=user.id)
                for cmd in commands
                if cmd and cmd.strip()
            ]

            for row in rows:
                exists = (
                    db.query(UserPermission.id)
                    .filter(
                        UserPermission.user_id == row.user_id,
                        UserPermission.command_name == row.command_name,
                    )
                    .first()
                )
                if not exists:
                    db.add(row)

            db.commit()

            logger.info(
                "Role assigned",
                extra={"user_id": user.id, "role_id": role.id, "role_name": role.name, "command_count": len(rows)},
            )

            return {"status": f"Role '{payload.role_name}' assigned to user '{payload.user}'"}
        finally:
            db.close()
