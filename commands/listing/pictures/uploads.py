from commands.base_command import BaseCommand
from pydantic import BaseModel, field_validator
from fastapi import UploadFile
from datetime import datetime
import os, uuid, shutil
from auth.db import SessionLocal
from sqlalchemy import text


class UploadListingPicturesPayload(BaseModel):
    listing_id: str

    @field_validator("listing_id")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Listing ID must not be empty")
        return v


class UploadListingPicturesCommand(BaseCommand):
    name = "listing/pictures/upload"
    schema = UploadListingPicturesPayload
    require_auth = True
    method = "post"
    type = "file_upload"
    upload_dir = "uploads/listings"
    static_mount = "/static/listings"

    async def execute(self, payload: UploadListingPicturesPayload, files: list[UploadFile], user_id: str):
        MAX_FILE_SIZE_MB = 50
        ALLOWED_TYPES = ["image/jpeg", "image/png", "image/webp"]

        saved_files = []
        db = SessionLocal()

        try:
            for idx, file in enumerate(files):
                contents = await file.read()
                file.file.seek(0)

                if file.content_type not in ALLOWED_TYPES:
                    raise ValueError(f"Unsupported file type: {file.content_type}")

                if len(contents) > MAX_FILE_SIZE_MB * 1024 * 1024:
                    raise ValueError(f"File {file.filename} exceeds 50MB limit")

                ext = file.filename.split('.')[-1].lower()
                filename = f"{uuid.uuid4()}.{ext}"
                path = os.path.join(self.upload_dir, filename)

                with open(path, "wb") as out:
                    shutil.copyfileobj(file.file, out)

                db.execute(text("""
                    INSERT INTO tbl_listing_pictures (
                        listing_id, user_id, filename, is_primary, sort_order, uploaded_at
                    )
                    VALUES (:listing_id, :user_id, :filename, FALSE, :sort_order, :uploaded_at)
                """), {
                    "listing_id": payload.listing_id,
                    "user_id": user_id,
                    "filename": filename,
                    "sort_order": idx,
                    "uploaded_at": datetime.utcnow()
                })

                saved_files.append({
                    "filename": filename,
                    "url": f"{self.static_mount}/{filename}"
                })

            db.commit()
            return {
                "status": "success",
                "files": saved_files
            }

        finally:
            db.close()
