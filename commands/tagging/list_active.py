# commands/tagging/list_active.py
from commands.base_command import BaseCommand
from fastapi import HTTPException
from auth.db import SessionLocal
from sqlalchemy import text
from datetime import datetime


class TagListActiveCommand(BaseCommand):
    name = "tagging/active"
    schema = None
    require_auth = True
    method = "GET"
    group = "Tagging"

    def run(self, payload):
        user_id = payload.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")

        with SessionLocal() as session:
            rows = session.execute(
                text(
                    """
                    SELECT
                        t.name         AS name,
                        t.color_hex    AS color_hex,
                        t.description  AS description,
                        t.is_active    AS is_active,
                        t.created_at   AS created_at,
                        t.updated_at   AS updated_at
                    FROM tbl_user_tags t
                    WHERE t.user_id = :uid
                      AND t.is_active = TRUE
                    ORDER BY t.created_at DESC, t.name ASC
                    """
                ),
                {"uid": user_id},
            ).mappings().all()

        def _iso(dt):
            return dt.isoformat() if isinstance(dt, datetime) else dt

        tags = [
            {
                "name": r["name"],
                "color_hex": r["color_hex"],
                "description": r["description"],
                "is_active": bool(r["is_active"]),
                "created_at": _iso(r["created_at"]),
                "updated_at": _iso(r["updated_at"]),
            }
            for r in rows
        ]

        return {
            "tags": tags,
        }
