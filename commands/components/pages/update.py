import os
import re
import json
import mimetypes
import time
import asyncio
from uuid import uuid4
from typing import Optional, List, Literal, Dict, Any

from fastapi import UploadFile, HTTPException
from pydantic import BaseModel, field_validator, Field

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_pages import CompPage

# logger fallback
try:
    from logger import logger
except Exception:
    import logging as _logging
    logger = _logging.getLogger("update_page_with_uploads")
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
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        parts = [str(p).strip() for p in value]
    else:
        parts = []
    out, seen = [], set()
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ---------------- Payload (only editable fields) ----------------
class UpdateCompPagesPayload(BaseModel):
    id: str
    name: Optional[str] = None
    description: Optional[str] = None

    tags: Optional[str] = None
    tags_mode: Literal["append", "replace", "remove"] = Field(default="replace")
    tags_clear: bool = False

    images_mode: Literal["append", "replace"] = Field(default="append")

    thumbnail_clear: bool = False
    images_clear: bool = False
    file_link_clear: bool = False

    # NEW: metadata (maps to CompPage.metadata_json / JSONB)
    metadata: Optional[Any] = None

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
        if v is None:
            return None
        return str(v).strip() or None

    @field_validator("metadata", mode="before")
    @classmethod
    def _metadata_in(cls, v):
        """
        Accept dict, None, or JSON string and normalize to dict/None.
        Same semantics as contents/layouts/auth/page create commands.
        """
        # Already correct
        if v is None or isinstance(v, dict):
            return v

        # JSON string from multipart/form-data
        if isinstance(v, str):
            raw = v.strip()
            if not raw:
                return None
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(f"metadata must be valid JSON if provided as string: {e}")

            if isinstance(parsed, dict):
                return parsed

            # Valid JSON but not object → wrap
            return {"value": parsed}

        # Fallback: best-effort cast to dict
        try:
            return dict(v)
        except Exception:
            raise ValueError("metadata must be a JSON object or JSON string")


class UpdatePageWithUploadsCommand(BaseCommand):
    """
    Partially updates a CompPage row:
      - name, description, tags, thumbnail, images, file_link, metadata_json, updated_by

    NOTE: We store the raw GCS keys in the DB and return signed URLs
    in the `gcs` block of the response for convenience.
    """
    name = "components/pages/update_with_uploads"
    schema = UpdateCompPagesPayload
    require_auth = True
    method = "put"
    type = "file_upload"
    group = "Page"

    file_fields = [
        ("thumbnail", False),
        ("images", True),
        ("file_link", False),
    ]

    base_folder = "uploads"
    MAX_IMAGE_MB = 50
    MAX_FILE_MB = 200
    IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
    ANY_FILE_TYPES = None

    async def execute(
        self,
        payload: UpdateCompPagesPayload,
        thumbnail: Optional[UploadFile] = None,
        images: Optional[List[UploadFile]] = None,
        file_link: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ):
        start_t = time.monotonic()
        logger.info("[pages.update] start id=%s user_id=%s", payload.id, user_id)

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        db = SessionLocal()
        try:
            row: Optional[CompPage] = db.query(CompPage).get(payload.id)
            if not row:
                raise HTTPException(status_code=404, detail="Page not found")

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
                    "[pages.update] _read file=%s size=%d ct=%s elapsed=%.3fs",
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

                # IMPORTANT: store the GCS key, not a URL
                return res["key"]

            # -------- scalar fields --------
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

            # -------- metadata_json (full replacement if provided) --------
            if payload.metadata is not None:
                row.metadata_json = payload.metadata or None

            # -------- thumbnail (file_link stored as key) --------
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
            current_images: List[str] = list(row.images or [])

            if payload.images_clear:
                current_images = []

            new_image_keys: List[str] = []
            if images:
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
                if payload.images_mode == "replace":
                    current_images = new_image_keys
                else:
                    current_images.extend(new_image_keys)

            if payload.images_clear or new_image_keys or (row.images is None):
                row.images = current_images

            # -------- main file_link (stored as key) --------
            if payload.file_link_clear:
                row.file_link = None

            if file_link:
                _, att_ct = await _read_and_check(file_link, self.MAX_FILE_MB, self.ANY_FILE_TYPES)
                att_name = make_uuid_name(file_link.filename, "file")
                att_key = await _upload_and_return_key(
                    file_link.file,
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
            logger.info("[pages.update] DB commit elapsed=%.3fs id=%s", time.monotonic() - t0, row.id)

            # -------- signed URLs for convenience --------
            signed_thumb = None
            signed_imgs: List[str] = []
            signed_file = None

            try:
                if row.thumbnail:
                    signed_thumb = gcs.signed_get_url(row.thumbnail, expires_seconds=3600)
            except Exception:
                try:
                    signed_thumb = f"https://storage.googleapis.com/{gcs.bucket_name}/{row.thumbnail}"
                except Exception:
                    signed_thumb = None

            try:
                for k in row.images or []:
                    try:
                        signed_imgs.append(gcs.signed_get_url(k, expires_seconds=3600))
                    except Exception:
                        try:
                            signed_imgs.append(f"https://storage.googleapis.com/{gcs.bucket_name}/{k}")
                        except Exception:
                            signed_imgs.append(k)
            except Exception:
                pass

            try:
                if row.file_link:
                    signed_file = gcs.signed_get_url(row.file_link, expires_seconds=3600)
            except Exception:
                try:
                    signed_file = f"https://storage.googleapis.com/{gcs.bucket_name}/{row.file_link}"
                except Exception:
                    signed_file = None

            elapsed_total = time.monotonic() - start_t
            logger.info("[pages.update] finished total_elapsed=%.3fs id=%s", elapsed_total, row.id)

            return {
                "status": "ok",
                "data": _comp_pages_to_dict(row),
                "gcs": {
                    "thumbnail": signed_thumb,
                    "images": signed_imgs,
                    "file_link": signed_file,
                    "page_folder": str(row.id),
                },
            }

        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            logger.exception("[pages.update] error - rolling back: %s", e)
            db.rollback()
            raise HTTPException(status_code=500, detail="Failed to update page")
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
                if file_link and getattr(file_link, "file", None) and not file_link.file.closed:
                    file_link.file.close()
            except Exception:
                pass


def _comp_pages_to_dict(m: CompPage) -> dict:
    return {
        "id": str(m.id) if getattr(m, "id", None) is not None else None,
        "name": m.name,
        "description": m.description,
        "thumbnail": m.thumbnail,
        "images": m.images,
        "template_id": str(m.template_id) if getattr(m, "template_id", None) else None,
        "file_link": m.file_link,
        "tags": getattr(m, "tags", []) or [],
        "metadata": getattr(m, "metadata_json", None) or {},
        "created_by": m.created_by,
        "updated_by": m.updated_by,
        "created_at": m.created_at.isoformat() if getattr(m, "created_at", None) else None,
        "updated_at": m.updated_at.isoformat() if getattr(m, "updated_at", None) else None,
    }
