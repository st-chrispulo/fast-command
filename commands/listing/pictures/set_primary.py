from commands.base_command import BaseCommand
from pydantic import BaseModel
from auth.db import SessionLocal
from sqlalchemy import text


class SetPrimaryPicturePayload(BaseModel):
    listing_id: str
    picture_id: int


class SetPrimaryListingPictureCommand(BaseCommand):
    name = "listing/pictures/set_primary"
    schema = SetPrimaryPicturePayload
    require_auth = True

    def run(self, payload: SetPrimaryPicturePayload):
        db = SessionLocal()
        try:
            picture_check = db.execute(
                text("""
                    SELECT 1 FROM tbl_listing_pictures
                    WHERE id = :picture_id AND listing_id = :listing_id
                """),
                {"picture_id": payload.picture_id, "listing_id": payload.listing_id}
            ).fetchone()

            if not picture_check:
                return {"error": "Picture does not belong to listing"}

            db.execute(
                text("UPDATE tbl_listing_pictures SET is_primary = FALSE WHERE listing_id = :listing_id"),
                {"listing_id": payload.listing_id}
            )

            db.execute(
                text("UPDATE tbl_listing_pictures SET is_primary = TRUE WHERE id = :picture_id"),
                {"picture_id": payload.picture_id}
            )

            db.commit()
            return {"status": "success", "primary_picture_id": payload.picture_id}
        finally:
            db.close()
