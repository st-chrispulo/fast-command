from commands.base_command import BaseCommand
from auth.db import SessionLocal
from fastapi import HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import text


class DeleteListingPayload(BaseModel):
    id: str

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Listing ID is required")
        return v


class DeleteListingCommand(BaseCommand):
    name = "listing/delete"
    schema = DeleteListingPayload
    require_auth = True

    def run(self, payload: DeleteListingPayload):
        db = SessionLocal()
        try:
            deleted = db.execute(
                text("DELETE FROM tbl_listings WHERE id = :id RETURNING id"),
                {"id": payload.id}
            ).fetchone()

            db.commit()

            if not deleted:
                raise HTTPException(status_code=404, detail="Listing not found")

            return {"status": "deleted", "id": payload.id}
        finally:
            db.close()
