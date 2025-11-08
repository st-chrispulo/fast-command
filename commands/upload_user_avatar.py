# commands/upload_user_avatar.py
from commands.base_command import BaseCommand
from pydantic import BaseModel
from fastapi import UploadFile
from datetime import datetime
import os
import uuid
import shutil
import asyncio
from auth.db import SessionLocal
from sqlalchemy import text
import mimetypes

# Optional Pillow check — best-effort. If Pillow isn't installed we skip strict validation.
try:
    from PIL import Image
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False


_FALLBACK_MIME = {
    ".webp": "image/webp",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
}


def _resolve_content_type(upload: UploadFile) -> str:
    if getattr(upload, "content_type", None):
        return upload.content_type
    name = getattr(upload, "filename", "") or ""
    ext = os.path.splitext(name)[1].lower()
    if ext in _FALLBACK_MIME:
        return _FALLBACK_MIME[ext]
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


class UploadUserAvatarPayload(BaseModel):
    # empty payload kept for compatibility
    pass


class UploadUserAvatarCommand(BaseCommand):
    name = "upload_user_avatar"
    schema = UploadUserAvatarPayload
    require_auth = True
    method = "post"
    type = "file_upload"

    # Local storage settings (can be overriden on the instance)
    upload_dir = "uploads/avatars"
    static_mount = "/static/avatars"

    async def execute(self, payload: UploadUserAvatarPayload, file: UploadFile, user_id: str):
        """
        Save uploaded avatar for user_id -> store filename in tbl_user_avatars.
        Returns {"status":"success","filename":..., "url": ...}

        Notes:
          - Streams to disk (no full-memory buffering)
          - Validates content-type against allowed list
          - Writes to temp file then atomically renames into final path
          - Removes previous avatar file after DB commit (best-effort)
        """
        MAX_FILE_SIZE_MB = 50
        ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}

        if file is None:
            raise ValueError("No file provided")

        # Resolve content type robustly
        content_type = _resolve_content_type(file)
        if content_type not in ALLOWED_TYPES:
            raise ValueError("Unsupported file type")

        # Ensure upload dir exists and use absolute path
        upload_dir = os.path.abspath(self.upload_dir)
        os.makedirs(upload_dir, exist_ok=True)

        # Determine extension safely
        _, ext = os.path.splitext(getattr(file, "filename", "") or "")
        ext = (ext or "").lower().lstrip(".")
        if not ext:
            # fallback from content type
            if content_type == "image/png":
                ext = "png"
            elif content_type in ("image/jpeg", "image/jpg"):
                ext = "jpg"
            elif content_type == "image/webp":
                ext = "webp"
            else:
                ext = "bin"

        # Generate target filename and temp path
        filename = f"{uuid.uuid4()}.{ext}"
        final_path = os.path.join(upload_dir, filename)
        temp_path = os.path.join(upload_dir, f".tmp-{uuid.uuid4()}")

        max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024

        # Blocking IO helper that streams file.file -> temp_path while enforcing size limit
        def _stream_save_sync(src_file, dest_path, max_bytes):
            total = 0
            chunk_size = 64 * 1024
            with open(dest_path, "wb") as out:
                while True:
                    chunk = src_file.read(chunk_size)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        # cleanup
                        out.close()
                        try:
                            os.remove(dest_path)
                        except Exception:
                            pass
                        raise ValueError(f"File exceeds {MAX_FILE_SIZE_MB}MB limit")
                    out.write(chunk)

        # run streaming in thread to avoid blocking event loop
        try:
            await asyncio.to_thread(_stream_save_sync, file.file, temp_path, max_bytes)
        except ValueError:
            raise
        except Exception as e:
            # cleanup any partial temp file
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass
            raise

        # Optional: validate that the written file is actually an image (Pillow)
        if _HAS_PIL:
            try:
                def _pil_verify(path):
                    with Image.open(path) as im:
                        im.verify()
                await asyncio.to_thread(_pil_verify, temp_path)
            except Exception:
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
                raise ValueError("Uploaded file is not a valid image")

        # DB upsert flow: read existing filename first (so we can delete it later), then upsert new row
        db = SessionLocal()
        old_filename = None
        try:
            # fetch existing filename if any
            row = db.execute(
                text("SELECT filename FROM tbl_user_avatars WHERE user_id = :user_id"),
                {"user_id": user_id}
            ).fetchone()
            if row and row[0]:
                old_filename = row[0]

            # Move temp file to final path atomically
            try:
                os.replace(temp_path, final_path)
            except Exception:
                # fallback to shutil.move
                shutil.move(temp_path, final_path)

            # Upsert: use postgres ON CONFLICT if available
            # We'll attempt a safe delete/insert only if ON CONFLICT isn't appropriate.
            # Using INSERT ... ON CONFLICT to simplify race windows.
            db.execute(text("""
                INSERT INTO tbl_user_avatars (user_id, filename, uploaded_at)
                VALUES (:user_id, :filename, :uploaded_at)
                ON CONFLICT (user_id) DO UPDATE
                  SET filename = EXCLUDED.filename,
                      uploaded_at = EXCLUDED.uploaded_at
            """), {
                "user_id": user_id,
                "filename": filename,
                "uploaded_at": datetime.utcnow()
            })
            db.commit()
        except Exception:
            # If DB failed, attempt to remove the final file to avoid orphan
            try:
                if os.path.exists(final_path):
                    os.remove(final_path)
            except Exception:
                pass
            raise
        finally:
            db.close()

        # After commit, remove old file if present and different from new filename
        if old_filename and old_filename != filename:
            try:
                old_path = os.path.join(upload_dir, old_filename)
                if os.path.exists(old_path):
                    os.remove(old_path)
            except Exception:
                # non-fatal; log if you have logger
                pass

        return {
            "status": "success",
            "filename": filename,
            "url": f"{self.static_mount.rstrip('/')}/{filename}"
        }
