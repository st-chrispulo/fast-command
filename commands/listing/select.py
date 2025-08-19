from commands.base_command import BaseCommand
from auth.db import SessionLocal
from sqlalchemy import text
from typing import Optional, List
from pydantic import BaseModel


class SelectListingPayload(BaseModel):
    min_price: Optional[float] = None
    max_price: Optional[float] = None


class SelectListingCommand(BaseCommand):
    name = "listing/select"
    schema = SelectListingPayload
    require_auth = True

    def run(self, payload: SelectListingPayload, user_id: str):
        db = SessionLocal()
        try:
            query = "SELECT * FROM tbl_listings WHERE user_id = :user_id"
            params = {"user_id": user_id}

            if payload.min_price is not None:
                query += " AND price >= :min_price"
                params["min_price"] = payload.min_price
            if payload.max_price is not None:
                query += " AND price <= :max_price"
                params["max_price"] = payload.max_price

            result = db.execute(text(query), params)
            listings = [dict(row._mapping) for row in result]

            return {"status": "success", "data": listings}
        finally:
            db.close()
