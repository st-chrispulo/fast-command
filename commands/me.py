from commands.base_command import BaseCommand
from fastapi import HTTPException
from auth.db import SessionLocal
from sqlalchemy import text
from datetime import datetime


def _mask_email(email: str) -> str:
    """
    Return a lightly masked email to avoid exposing full PII.
    e.g., "j*****e@gmail.com"
    """
    if not email or "@" not in email:
        return email or ""
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked_local = local[0] + "*" * max(0, len(local) - 1)
    else:
        masked_local = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{masked_local}@{domain}"


class MeCommand(BaseCommand):
    name = "me"
    schema = None
    require_auth = True
    method = "GET"

    def run(self, payload):
        user_id = payload.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")

        with SessionLocal() as session:
            row = session.execute(
                text(
                    """
                    SELECT
                        u.id                  AS user_id,
                        u.username            AS username,
                        u.email               AS email,
                        u.created_at          AS created_at,

                        g.login               AS gh_login,
                        g.name                AS gh_name,
                        g.email               AS gh_email,
                        g.avatar_url          AS gh_avatar_url,
                        g.installed_at        AS gh_installed_at,
                        g.last_synced_at      AS gh_last_synced_at
                    FROM tbl_users u
                    LEFT JOIN tbl_user_github g
                      ON g.user_id = u.id
                    WHERE u.id = :uid
                    """
                ),
                {"uid": user_id},
            ).mappings().first()

        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        # Build a non-sensitive user payload (no password)
        user_payload = {
            "id": row["user_id"],
            "username": row["username"],
            # If you prefer not to return any email, remove the next two lines:
            "email_masked": _mask_email(row["email"]),
            "created_at": row["created_at"].isoformat() if isinstance(row["created_at"], datetime) else row["created_at"],
        }

        # GitHub summary (no tokens, no raw ids unless you want to expose login only)
        has_github = row["gh_login"] is not None
        github_payload = None
        if has_github:
            github_payload = {
                "login": row["gh_login"],
                "name": row["gh_name"],
                "email_masked": _mask_email(row["gh_email"]) if row["gh_email"] else None,
                "avatar_url": row["gh_avatar_url"],
                "installed_at": row["gh_installed_at"].isoformat() if isinstance(row["gh_installed_at"], datetime) else row["gh_installed_at"],
                "last_synced_at": row["gh_last_synced_at"].isoformat() if isinstance(row["gh_last_synced_at"], datetime) else row["gh_last_synced_at"],
            }

        return {
            "user": user_payload,
            "has_github": has_github,
            "github": github_payload,
        }
