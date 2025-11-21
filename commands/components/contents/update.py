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


def _normalize_tags(value) -> List[str]:
    """
    Normalize tags to a clean list:
      - Accepts "a, b" or ["a","b"] or None
      - Strips whitespace, drops blanks, de-dupes while preserving order
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        parts = [str(p).strip() for p in value]
    else:
        parts = []
    out: List[str] = []
    seen = set()
    for p in parts:
        if not p:
            continue
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ---------------- Payload (only editable fields) ----------------
class UpdateCompContentsPayload(BaseModel):
    # We accept string to avoid hard pydantic UUID parsing errors
    id: str

    # Editable scalar fields (omit to leave unchanged)
    name: Optional[str] = None
    description: Optional[str] = None

    # Tags editing
    # CHANGED: accept a single comma-separated string (e.g., "x, y, z")
    tags: Optional[str] = None
    tags_mode: Literal["append", "replace", "remove"] = Field(default="replace")
    tags_clear: bool = False  # clear all tags regardless of tags/tags_mode

    # Images behavior (for file uploads only)
    images_mode: Literal["append", "replace"] = Field(default="append")

    # Explicit clear flags (default False -> do nothing if file not provided)
    thumbnail_clear: bool = False
    images_clear: bool = False         # clear all existing images (unless new ones provided with replace)
    file_link_clear: bool = False      # clear main file (attachment)

    @field_validator("name")
    @classmethod
    def _name_trim(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("description", mode="before")
    @classmethod
    def _desc_trim(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("tags", mode="before")
    @classmethod
    def _tags_in(cls, v):
        # CHANGED: coerce to a single trimmed string or None
        if v is None:
            return None
        return str(v).strip() or None


class UpdateComponentWithUploadsCommand(BaseCommand):
    """
    Partially updates a CompContent row. Only these fields are mutable:
      - name, description, tags, thumbnail, images, file_link, updated_by

    Rules:
      - If no file is attached and no *_clear flag is set, the file fields are left unchanged.
      - To clear a file field without uploading, set its clear flag to True.
      - For images:
          * images_mode = "append" (default): appends newly uploaded images.
          * images_mode = "replace": replaces current images with only the newly uploaded ones.
          * images_clear = True: clears all existing images.
      - For tags (TEXT[]):
          * tags_clear = True: clears all existing tags.
          * tags_mode = "replace": replace with provided tags.
          * tags_mode = "append": add provided tags (no duplicates).
          * tags_mode = "remove": remove any provided tags from existing.
      - For main file:
          * multipart field is "attachment"
          * uploaded file is stored to GCS and its key is saved in row.file_link
    """

    name = "components/contents/update_with_uploads"
    schema = UpdateCompContentsPayload
    require_auth = True
    method = "put"
    type = "file_upload"
    group = "Content"

    # IMPORTANT: these names must match the multipart fields, not the JSON body
    file_fields = [
        ("thumbnail", False),
        ("images", True),
        ("attachment", False),
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
        attachment: Optional[UploadFile] = None,
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

            async def _upload_and_return_key(fileobj, filename: str, key_prefix: str, content_type: str) -> str:
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

                # IMPORTANT: store the GCS key, not a full URL
                return res["key"]

            # -------- scalar fields (leave unchanged if not provided) --------
            if payload.name is not None and payload.name.strip():
                row.name = payload.name.strip()
            if payload.description is not None:
                row.description = payload.description

            # -------- tags (append/replace/remove/clear) --------
            current_tags: List[str] = list(row.tags or [])
            if payload.tags_clear:
                current_tags = []

            incoming: List[str] = _normalize_tags(payload.tags)

            if incoming:
                if payload.tags_mode == "replace":
                    current_tags = incoming
                elif payload.tags_mode == "append":
                    seen = set(current_tags)
                    for t in incoming:
                        if t not in seen:
                            current_tags.append(t)
                            seen.add(t)
                elif payload.tags_mode == "remove":
                    remove_set = set(incoming)
                    current_tags = [t for t in current_tags if t not in remove_set]
            else:
                # If tags_mode=replace AND user sent empty string and not tags_clear,
                # interpret as "no-op" (do not wipe). Leave as-is unless tags_clear was set.
                if payload.tags_mode == "replace" and not payload.tags_clear:
                    pass

            if payload.tags_clear or payload.tags is not None or row.tags is None:
                row.tags = current_tags

            # -------- thumbnail (replace only if file provided; clear only if flag True) --------
            if payload.thumbnail_clear:
                row.thumbnail = None

            if thumbnail:
                _, thumb_ct = await _read_and_check(thumbnail, self.MAX_IMAGE_MB, self.IMAGE_TYPES)
                thumb_name = make_uuid_name(thumbnail.filename, "thumbnail")
                thumb_key = await _upload_and_return_key(
                    thumbnail.file,
                    thumb_name,
                    f"{dest_prefix}/thumbnail",
                    thumb_ct,
                )
                row.thumbnail = thumb_key

            # -------- images (append/replace/clear) --------
            # -------- images (replace/clear) --------
            current_images: List[str] = list(row.images or [])

            if payload.images_clear:
                current_images = []

            new_image_keys: List[str] = []
            if images:
                logger.info("[update_with_uploads] uploading %d image(s)", len(images))
                for idx, img in enumerate(images):
                    _, img_ct = await _read_and_check(img, self.MAX_IMAGE_MB, self.IMAGE_TYPES)
                    img_name = make_uuid_name(img.filename, f"img{idx:03d}")
                    img_key = await _upload_and_return_key(
                        img.file,
                        img_name,
                        f"{dest_prefix}/images",
                        img_ct,
                    )
                    new_image_keys.append(img_key)

            if new_image_keys:
                # 🔴 replace old images with new set
                current_images = new_image_keys

            if payload.images_clear or new_image_keys or (row.images is None):
                row.images = current_images

            # -------- main file (file_link) via "attachment" --------
            if payload.file_link_clear:
                row.file_link = None

            if attachment:
                _, att_ct = await _read_and_check(attachment, self.MAX_FILE_MB, self.ANY_FILE_TYPES)
                att_name = make_uuid_name(attachment.filename or "file", "file")
                att_key = await _upload_and_return_key(
                    attachment.file,
                    att_name,
                    f"{dest_prefix}/files",
                    att_ct,
                )
                row.file_link = att_key

            row.updated_by = user_id

            # -------- persist --------
            t0 = time.monotonic()
            db.add(row)
            db.commit()
            db.refresh(row)
            logger.info("[update_with_uploads] DB commit elapsed=%.3fs id=%s", time.monotonic() - t0, row.id)

            elapsed_total = time.monotonic() - start_t
            logger.info("[update_with_uploads] finished total_elapsed=%.3fs id=%s", elapsed_total, row.id)

            # Build signed URLs for convenience (like create_with_uploads)
            signed_thumb = None
            signed_imgs: List[str] = []
            signed_file = None
            try:
                if row.thumbnail:
                    signed_thumb = gcs.signed_get_url(row.thumbnail, expires_seconds=3600)
            except Exception:
                pass
            try:
                for k in row.images or []:
                    try:
                        signed_imgs.append(gcs.signed_get_url(k, expires_seconds=3600))
                    except Exception:
                        signed_imgs.append(f"https://storage.googleapis.com/{gcs.bucket_name}/{k}")
            except Exception:
                pass
            try:
                if row.file_link:
                    signed_file = gcs.signed_get_url(row.file_link, expires_seconds=3600)
            except Exception:
                pass

            return {
                "status": "ok",
                "data": _comp_contents_to_dict(row),
                "gcs": {
                    "thumbnail": signed_thumb,
                    "images": signed_imgs,
                    "file_link": signed_file,
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
                if attachment and getattr(attachment, "file", None) and not attachment.file.closed:
                    attachment.file.close()
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
        "tags": getattr(m, "tags", []) or [],
        "created_by": m.created_by,
        "updated_by": m.updated_by,
        "created_at": m.created_at.isoformat() if getattr(m, "created_at", None) else None,
        "updated_at": m.updated_at.isoformat() if getattr(m, "updated_at", None) else None,
    }
