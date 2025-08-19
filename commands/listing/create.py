from commands.base_command import BaseCommand
from auth.db import SessionLocal
from pydantic import BaseModel, field_validator
from sqlalchemy import text
import uuid
from typing import Optional, Dict


class CreateListingPayload(BaseModel):
    title: str
    address: str
    price: float
    bedrooms: Optional[int] = None
    bathrooms: Optional[int] = None
    attributes: Optional[Dict] = {}

    @field_validator("title", "address")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Field must not be empty")
        return v


class CreateListingCommand(BaseCommand):
    name = "listing/create"
    schema = CreateListingPayload
    require_auth = True

    def run(self, payload: CreateListingPayload, user_id: str):
        db = SessionLocal()
        try:
            listing_id = str(uuid.uuid4())
            db.execute(
                text("""
                    INSERT INTO tbl_listings (
                        id, user_id, title, address, price,
                        bedrooms, bathrooms, attributes
                    )
                    VALUES (
                        :id, :user_id, :title, :address, :price,
                        :bedrooms, :bathrooms, :attributes
                    )
                """),
                {
                    "id": listing_id,
                    "user_id": user_id,
                    "title": payload.title,
                    "address": payload.address,
                    "price": payload.price,
                    "bedrooms": payload.bedrooms,
                    "bathrooms": payload.bathrooms,
                    "attributes": payload.attributes
                }
            )
            db.commit()
            return {"status": "success", "id": listing_id}
        finally:
            db.close()
