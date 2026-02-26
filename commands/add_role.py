from __future__ import annotations

from typing import List, Optional

from fastapi import HTTPException
from pydantic import BaseModel, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.tbl_roles import Role

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.roles.add")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class AddRolePayload(BaseModel):
    """Payload for creating a role."""

    name: str
    description: Optional[str] = None
    command_names: List[str]

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Role name must not be empty")
        return v

    @field_validator("command_names")
    @classmethod
    def validate_commands(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("At least one command must be assigned")

        cleaned: List[str] = []
        seen = set()

        for cmd in v:
            c = (cmd or "").strip()
            if not c:
                continue
            if c not in seen:
                cleaned.append(c)
                seen.add(c)

        if not cleaned:
            raise ValueError("At least one command must be assigned")

        return cleaned


class AddRoleCommand(BaseCommand):
    """Creates a role with allowed command names."""

    name = "role/add"
    schema = AddRolePayload

    def run(self, payload: AddRolePayload):
        db = SessionLocal()
        try:
            existing = db.query(Role.id).filter(Role.name == payload.name).first()
            if existing:
                raise HTTPException(status_code=400, detail=f"Role '{payload.name}' already exists.")

            role = Role(
                name=payload.name,
                description=(payload.description.strip() if payload.description else None),
                command_names=payload.command_names,
            )

            db.add(role)
            db.commit()

            logger.info("Role created", extra={"role_id": role.id, "name": role.name})

            return {"status": f"Role '{payload.name}' created successfully"}
        finally:
            db.close()
