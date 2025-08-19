from commands.base_command import BaseCommand
from pydantic import BaseModel, field_validator
from auth.db import SessionLocal
from sqlalchemy import text
import os
from typing import List


class DeleteListingPicturesPayload(BaseModel):
    ids: List[int]


    @field_validator("ids")
    @classmethod
    def check_non_empty(cls, v: List[int]) -> List[int]:
        if not v:
            raise ValueError("At least one picture ID is required")
        return v


class DeleteListingPicturesCommand(BaseCommand):
    name = "listing/pictures/delete"
    schema = DeleteListingPicturesPayload
    require_auth = True

    upload_dir = "uploads/listings"

    def run(self, payload: DeleteListingPicturesPayload):
        db = SessionLocal()
        try:

            results = db.execute(
                text(f"""
                    SELECT id, filename FROM tbl_listing_pictures
                    WHERE id = ANY(:ids)
                """),
                {"ids": payload.ids}
            ).fetchall()

            if not results:
                return {"error": "No matching pictures found"}

            found_ids = [row[0] for row in results]
            filenames = [row[1] for row in results]

            for fname in filenames:
                path = os.path.join(self.upload_dir, fname)
                if os.path.exists(path):
                    os.remove(path)

            db.execute(
                text("DELETE FROM tbl_listing_pictures WHERE id = ANY(:ids)"),
                {"ids": found_ids}
            )
            db.commit()

            return {
                "status": "deleted",
                "deleted_ids": found_ids
            }

        finally:
            db.close()
