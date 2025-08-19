from commands.base_command import BaseCommand
from auth.db import SessionLocal
from fastapi import HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import text
from typing import Optional, Dict


class UpdateListingPayload(BaseModel):
    id: str
    title: Optional[str] = None
    address: Optional[str] = None
    price: Optional[float] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[int] = None
    attributes: Optional[Dict] = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Listing ID is required")
        return v


class UpdateListingCommand(BaseCommand):
    name = "listing/update"
    schema = UpdateListingPayload
    require_auth = True

    def run(self, payload: UpdateListingPayload):
        db = SessionLocal()
        try:
            existing = db.execute(
                text("SELECT 1 FROM tbl_listings WHERE id = :id"),
                {"id": payload.id}
            ).fetchone()

            if not existing:
                raise HTTPException(status_code=404, detail="Listing not found")

            update_fields = []
            params = {"id": payload.id}

            for field in ["title", "address", "price", "bedrooms", "bathrooms", "attributes"]:
                value = getattr(payload, field)
                if value is not None:
                    update_fields.append(f"{field} = :{field}")
                    params[field] = value

            if update_fields:
                db.execute(
                    text(f"""
                        UPDATE tbl_listings
                        SET {', '.join(update_fields)},
                            updated_at = NOW()
                        WHERE id = :id
                    """),
                    params
                )
                db.commit()

            return {"status": "success", "id": payload.id}
        finally:
            db.close()
