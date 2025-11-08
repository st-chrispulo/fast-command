# /commands/components/content/create.py
import json
from typing import Any, List, Optional, Union
from uuid import UUID

from pydantic import BaseModel, field_validator
from fastapi import HTTPException

from commands.base_command import BaseCommand
from auth.db import SessionLocal

# SQLAlchemy model
from models.components.tbl_comp_contents import CompContent


# -------------------------
# Payload
# -------------------------
class CreateCompContentsPayload(BaseModel):
    name: str
    description: Optional[str] = None
    thumbnail: Optional[str] = None
    images: Optional[Union[List[Any], dict, str]] = None  # JSONB-friendly
    template_id: Optional[UUID] = None
    file_link: Optional[str] = None

    # If your auth flow attaches the current user, we'll use it.
    # Still allow explicit override for service calls/testing.
    created_by: Optional[int] = None
    updated_by: Optional[int] = None

    @field_validator("name")
    @classmethod
    def _name_trim(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("name is required")
        return v

    @field_validator("thumbnail", "file_link", "description", mode="before")
    @classmethod
    def _strip_optional(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("images", mode="before")
    @classmethod
    def _normalize_images(cls, v):
        """
        Accept:
          - list/dict  → pass through
          - JSON-encoded string → parse to list/dict
          - None → None
        """
        if v is None:
            return None
        if isinstance(v, (list, dict)):
            return v
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                # If a single URL string was sent, store as single-item list
                return [s]
        return v


# -------------------------
# Command
# -------------------------
class CreateCompContentsCommand(BaseCommand):
    """
    Creates a component contents row.

    Auth:
      - If your BaseCommand provides current user (e.g. self.user_id),
        we default created_by/updated_by to that user unless payload overrides.

    Returns:
      {
        "status": "ok",
        "data": { ...row }
      }
    """
    name = "components/contents/create"
    schema = CreateCompContentsPayload
    require_auth = True  # flip to False if you want public creation

    def run(self, payload: CreateCompContentsPayload):
        db = SessionLocal()
        try:
            # Prefer auth context for created_by/updated_by
            current_uid = getattr(self, "user_id", None) or getattr(self, "actor_id", None)
            created_by = payload.created_by if payload.created_by is not None else current_uid
            updated_by = payload.updated_by if payload.updated_by is not None else current_uid

            # Basic guard: you required auth above, so ensure we have a user id
            if self.require_auth and created_by is None:
                raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

            row = CompContent(
                name=payload.name,
                description=payload.description,
                thumbnail=payload.thumbnail,
                images=payload.images,            # JSONB
                template_id=payload.template_id,  # UUID or None
                file_link=payload.file_link,
                created_by=created_by,
                updated_by=updated_by,
            )

            db.add(row)
            db.commit()
            db.refresh(row)

            return {
                "status": "ok",
                "data": _comp_contents_to_dict(row),
            }
        finally:
            db.close()


# -------------------------
# Helpers
# -------------------------
def _comp_contents_to_dict(m: CompContent) -> dict:
    # SQLAlchemy model → serializable dict
    return {
        "id": str(m.id) if m.id is not None else None,
        "name": m.name,
        "description": m.description,
        "thumbnail": m.thumbnail,
        "images": m.images,  # already JSON-serializable
        "template_id": str(m.template_id) if m.template_id else None,
        "file_link": m.file_link,
        "created_by": m.created_by,
        "updated_by": m.updated_by,
        "created_at": m.created_at.isoformat() if getattr(m, "created_at", None) else None,
        "updated_at": m.updated_at.isoformat() if getattr(m, "updated_at", None) else None,
    }


if __name__ == "__main__":
    """
    Quick local smoke test:
      - Simulates an authenticated caller (user_id=1)
      - Builds a payload (images can be JSON string, list, or dict)
      - Executes the command and pretty-prints the result

    Prereqs in your DB:
      - tbl_users with an id=1 row (or change user_id below)
      - tbl_comp_contents table created (migration applied)
    """
    from pprint import pprint
    from fastapi import HTTPException

    # Example payload (adjust as needed)
    sample_payload = {
        "name": "Sample Component (CLI)",
        "description": "Created via __main__ smoke test",
        "thumbnail": "https://cdn.example.com/thumb.png",
        # Accepts JSON string, list, or dict
        "images": '["https://cdn.example.com/img1.png","https://cdn.example.com/img2.png"]',
        # Put a UUID string here if you want to set it, or leave None
        "template_id": None,
        "file_link": "https://cdn.example.com/file.zip",
        # You can explicitly set these, or let the command use self.user_id
        # "created_by": 1,
        # "updated_by": 1,
    }

    try:
        payload = CreateCompContentsPayload(**sample_payload)

        # Instantiate command and simulate auth context
        cmd = CreateCompContentsCommand()
        setattr(cmd, "user_id", 1)  # simulate an authenticated caller

        result = cmd.run(payload)
        pprint(result)
        print("\n✅ CreateCompContentsCommand finished.")
    except HTTPException as e:
        print(f"\n❌ HTTPException: {e.status_code} {e.detail}")
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")

