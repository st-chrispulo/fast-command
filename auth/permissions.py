from __future__ import annotations

from sqlalchemy.orm import Session

from models.tbl_user_permissions import UserPermission


def has_permission(db: Session, user_id: int, command: str) -> bool:
    """Return True if the user has permission for the given command."""
    command = (command or "").strip()
    if not command:
        return False

    hit = (
        db.query(UserPermission.id)
        .filter(UserPermission.user_id == user_id, UserPermission.command_name == command)
        .limit(1)
        .scalar()
    )
    return bool(hit)
