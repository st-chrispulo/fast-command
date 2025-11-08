# commands/components/content/update.py
import os
import re
import mimetypes
import time
import asyncio
from uuid import uuid4
from typing import Optional, List, Literal

from fastapi import UploadFile, HTTPException
from pydantic import BaseModel, field_validator, Field

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_contents import CompContent

# logger fallback
try:
    from logger import logger
except Exception:
    import logging as _logging
    logger = _logging.getLogger("update_component_with_uploads")
    if not logger.handlers:
        handler = _logging.StreamHandler()
        handler.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(_logging.DEBUG)

_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
_FALLBACK_MIME = {
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}

def _safe_stem(name: str, default_stem: str) -> str:
    stem, _ = os.path.splitext(name or "")
    stem = (stem or default_stem).strip()
    stem = _SAFE_CHARS_RE.sub("_", stem) or default_stem
    return stem

def make_uuid_name(filename: str, default_stem: str) -> str:
    stem = _safe_stem(filename, default_stem)
    ext = (os.path.splitext(filename or "")[1] or "").lower().lstrip(".") or "bin"
    return f"{stem}.{uuid4()}.{ext}"

def _resolve_content_type(upload: UploadFile) -> str:
    if getattr(upload, "content_type", None):
        return upload.content_type
    name = getattr(upload, "filename", "") or ""
    ext = os.path.splitext(name)[1].lower()
    if ext in _FALLBACK_MIME:
        return _FALLBACK_MIME[ext]
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


# ---------------- Payload (only editable fields) ----------------
class UpdateCompContentsPayload(BaseModel):
    # We accept string to avoid hard pydantic UUID parsing errors
    id: str

    # Editable scalar fields (omit to leave unchanged)
    name: Optional[str] = None
    description: Optional[str] = None
    updated_by: Optional[int] = None

    # Images behavior (for file uploads only)
    images_mode: Literal["append", "replace"] = Field(default="append")

    # Explicit clear flags (default False -> do nothing if file not provided)
    thumbnail_clear: bool = False
    images_clear: bool = False         # clear all existing images (unless new ones provided with replace)
    file_link_clear: bool = False

    @field_validator("name")
    @classmethod
    def _name_trim(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("description", mode="before")
    @classmethod
    def _desc_trim(cls, v):
        return v.strip() if isinstance(v, str) else v


class UpdateComponentWithUploadsCommand(BaseCommand):
    """
    Partially updates a CompContent row. Only these fields are mutable:
      - name, description, thumbnail, images, file_link, updated_by

    Rules:
      - If no file is attached and no *_clear flag is set, the field is left unchanged.
      - To clear a field without uploading, set its clear flag to True.
      - For images:
          * images_mode = "append" (default): appends newly uploaded images.
          * images_mode = "replace": replaces current images with only the newly uploaded ones.
          * images_clear = True: clears all existing images; if also uploading and mode=append,
            result is just the newly uploaded ones.

    File fields (multipart/form-data):
      - thumbnail: single UploadFile (image)
      - images:   multiple UploadFile (images)
      - file_link: single UploadFile (any type)
    """

    name = "components/contents/update_with_uploads"
    schema = UpdateCompContentsPayload
    require_auth = True
    method = "put"
    type = "file_upload"

    # IMPORTANT: these names must match the multipart fields, not the JSON body
    file_fields = [
        ("thumbnail", False),
        ("images", True),
        ("file_link", False),
    ]

    base_folder = "uploads"

    # Limits + types
    MAX_IMAGE_MB = 50
    MAX_FILE_MB = 200
    IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
    ANY_FILE_TYPES = None  # allow any

    async def execute(
        self,
        payload: UpdateCompContentsPayload,
        thumbnail: Optional[UploadFile] = None,
        images: Optional[List[UploadFile]] = None,
        file_link: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ):
        start_t = time.monotonic()
        logger.info("[update_with_uploads] start id=%s user_id=%s", payload.id, user_id)

        db = SessionLocal()
        try:
            # Fetch row; ids are UUID in DB, but we store as text to query
            row: Optional[CompContent] = db.query(CompContent).get(payload.id)
            if not row:
                raise HTTPException(status_code=404, detail="Content not found")

            # Auth context
            current_uid = getattr(self, "user_id", None) or getattr(self, "actor_id", None)
            if self.require_auth and (payload.updated_by is None and current_uid is None):
                raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

            gcs = get_gcs()
            dest_prefix = f"{self.base_folder}/{row.id}"

            async def _read_and_check(f: UploadFile, max_mb: int, allowed: Optional[set]):
                if f is None:
                    return None, None
                t0 = time.monotonic()
                data = await f.read()
                try:
                    f.file.seek(0)
                except Exception:
                    pass
                ct = _resolve_content_type(f)
                size = len(data)
                logger.debug(
                    "[update_with_uploads] _read_and_check file=%s size=%d ct=%s elapsed=%.3fs",
                    getattr(f, "filename", None), size, ct, time.monotonic() - t0
                )
                if size == 0:
                    raise ValueError(f"File '{f.filename}' is empty")
                if size > max_mb * 1024 * 1024:
                    raise ValueError(f"File '{f.filename}' exceeds {max_mb}MB limit")
                if allowed is not None and ct not in allowed:
                    raise ValueError(f"Unsupported content type '{ct}' for '{f.filename}'")
                return data, ct

            async def _upload_return_url(fileobj, filename: str, key_prefix: str, content_type: str) -> str:
                # Try positional signature first; fallback to kwargs
                try:
                    res = await asyncio.to_thread(
                        gcs.upload_fileobj,
                        fileobj,
                        filename,
                        key_prefix,
                        False,
                        content_type,
                    )
                except TypeError:
                    res = await asyncio.to_thread(
                        gcs.upload_fileobj,
                        fileobj=fileobj,
                        filename=filename,
                        dest_prefix=key_prefix,
                        public=False,
                        content_type=content_type,
                    )

                if not res or not res.get("ok"):
                    raise RuntimeError("Upload failed")

                if res.get("public_url"):
                    return res["public_url"]

                canonical = f"https://storage.googleapis.com/{res['bucket']}/{res['key']}"
                try:
                    return gcs.signed_get_url(res["key"], expires_seconds=3600)
                except Exception:
                    return canonical

            # -------- scalar fields (leave unchanged if not provided) --------
            if payload.name is not None and payload.name.strip():
                row.name = payload.name.strip()
            if payload.description is not None:
                row.description = payload.description

            # -------- thumbnail (replace only if file provided; clear only if flag True) --------
            if payload.thumbnail_clear:
                row.thumbnail = None

            if thumbnail:
                _, thumb_ct = await _read_and_check(thumbnail, self.MAX_IMAGE_MB, self.IMAGE_TYPES)
                thumb_name = make_uuid_name(thumbnail.filename, "thumbnail")
                url = await _upload_return_url(thumbnail.file, thumb_name, f"{dest_prefix}/thumbnail", thumb_ct)
                row.thumbnail = url

            # -------- images (append/replace/clear) --------
            current_images: List[str] = list(row.images or [])

            # Clear all images if requested
            if payload.images_clear:
                current_images = []

            new_image_urls: List[str] = []
            if images:
                logger.info("[update_with_uploads] uploading %d image(s)", len(images))
                for idx, img in enumerate(images):
                    _, img_ct = await _read_and_check(img, self.MAX_IMAGE_MB, self.IMAGE_TYPES)
                    img_name = make_uuid_name(img.filename, f"img{idx:03d}")
                    url = await _upload_return_url(img.file, img_name, f"{dest_prefix}/images", img_ct)
                    if url:
                        new_image_urls.append(url)

            if new_image_urls:
                if payload.images_mode == "replace":
                    current_images = new_image_urls
                else:
                    current_images.extend(new_image_urls)

            # Only assign back if changed (protects from accidental nulling)
            if payload.images_clear or new_image_urls or (row.images is None):
                row.images = current_images

            # -------- file_link (replace only if file provided; clear only if flag True) --------
            if payload.file_link_clear:
                row.file_link = None

            if file_link:
                _, att_ct = await _read_and_check(file_link, self.MAX_FILE_MB, self.ANY_FILE_TYPES)
                att_name = make_uuid_name(file_link.filename, "file")
                url = await _upload_return_url(file_link.file, att_name, f"{dest_prefix}/files", att_ct)
                row.file_link = url

            # -------- updated_by --------
            if payload.updated_by is not None:
                row.updated_by = payload.updated_by
            elif current_uid is not None:
                row.updated_by = current_uid

            # -------- persist --------
            t0 = time.monotonic()
            db.add(row)
            db.commit()
            db.refresh(row)
            logger.info("[update_with_uploads] DB commit elapsed=%.3fs id=%s", time.monotonic() - t0, row.id)

            elapsed_total = time.monotonic() - start_t
            logger.info("[update_with_uploads] finished total_elapsed=%.3fs id=%s", elapsed_total, row.id)

            return {
                "status": "ok",
                "data": _comp_contents_to_dict(row),
                "gcs": {
                    "thumbnail": row.thumbnail,
                    "images": row.images or [],
                    "file_link": row.file_link,
                    "content_folder": str(row.id),
                },
            }

        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            logger.exception("[update_with_uploads] error - rolling back: %s", e)
            db.rollback()
            raise HTTPException(status_code=500, detail="Failed to update content")
        finally:
            db.close()
            # best-effort close file handles
            try:
                if thumbnail and getattr(thumbnail, "file", None) and not thumbnail.file.closed:
                    thumbnail.file.close()
            except Exception:
                pass
            try:
                for uf in images or []:
                    if uf and getattr(uf, "file", None) and not uf.file.closed:
                        uf.file.close()
            except Exception:
                pass
            try:
                if file_link and getattr(file_link, "file", None) and not file_link.file.closed:
                    file_link.file.close()
            except Exception:
                pass


def _comp_contents_to_dict(m: CompContent) -> dict:
    return {
        "id": str(m.id) if getattr(m, "id", None) is not None else None,
        "name": m.name,
        "description": m.description,
        "thumbnail": m.thumbnail,
        "images": m.images,
        "template_id": str(m.template_id) if getattr(m, "template_id", None) else None,
        "file_link": m.file_link,
        "created_by": m.created_by,
        "updated_by": m.updated_by,
        "created_at": m.created_at.isoformat() if getattr(m, "created_at", None) else None,
        "updated_at": m.updated_at.isoformat() if getattr(m, "updated_at", None) else None,
    }
