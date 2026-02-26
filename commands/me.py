from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.tbl_user_github import UserGithub
from models.tbl_users import User

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.authentications.me")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return email or ""
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked_local = local[0] + "*" * max(0, len(local) - 1)
    else:
        masked_local = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{masked_local}@{domain}"


def _iso(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    return v


class MeCommand(BaseCommand):
    name = "me"
    schema = None
    require_auth = True
    method = "GET"

    def run(self, payload):
        user_id = payload.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")

        db = SessionLocal()
        try:
            user = (
                db.query(User)
                .filter(User.id == int(user_id))
                .first()
            )

            if not user:
                raise HTTPException(status_code=404, detail="User not found")

            gh = (
                db.query(UserGithub)
                .filter(UserGithub.user_id == user.id)
                .first()
            )

            user_payload = {
                "id": user.id,
                "username": user.username,
                "email_masked": _mask_email(user.email),
                "created_at": _iso(user.created_at),
            }

            has_github = gh is not None
            github_payload = None
            if has_github:
                github_payload = {
                    "login": gh.login,
                    "name": gh.name,
                    "email_masked": _mask_email(str(gh.email)) if gh.email else None,
                    "avatar_url": gh.avatar_url,
                    "installed_at": _iso(gh.installed_at),
                    "last_synced_at": _iso(gh.last_synced_at),
                }

            logger.info("Me fetched", extra={"user_id": user.id, "has_github": has_github})

            return {
                "user": user_payload,
                "has_github": has_github,
                "github": github_payload,
            }
        finally:
            db.close()
