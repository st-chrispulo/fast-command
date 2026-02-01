# commands/components/navigations/update_with_uploads.py

import os
import re
import json
import mimetypes
import time
import asyncio
from uuid import uuid4
from typing import Optional, List, Any, Dict, Literal

from fastapi import UploadFile, HTTPException
from pydantic import BaseModel, field_validator, Field
from uuid import UUID

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_navigations import CompNavigation

# logger fallback
try:
    from logger import logger
except Exception:
    import logging as _logging
    logger = _logging.getLogger("navigations_update_with_uploads")
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
    ".svg": "image/svg+xml",
    ".js": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".html": "text/html",
    ".txt": "text/plain",
}
MAX_IMAGE_MB = 50
MAX_FILE_MB = 200
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
ANY_FILE_TYPES = None  # allow any (validate size only)


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


# ---------------- Payload ----------------

class UpdateCompNavigationsPayload(BaseModel):
    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    template_id: Optional[UUID] = None

    # NEW (per model/migration)
    group_id: Optional[UUID] = None
    sub_type: Optional[str] = None

    tags: Optional[str] = None
    tags_mode: Literal["append", "replace", "remove"] = Field(default="replace")
    tags_clear: bool = False

    images_mode: Literal["append", "replace"] = Field(default="append")

    thumbnail_clear: bool = False
    images_clear: bool = False

    # UPDATED: single file_link (TEXT), not JSONB file_links
    file_link_clear: bool = False

    # NEW: metadata controls (JSONB metadata_json)
    metadata: Optional[Any] = None   # accepts dict or JSON string
    metadata_clear: bool = False     # clear metadata_json when True

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

    @field_validator("sub_type", mode="before")
    @classmethod
    def _sub_type_in(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("metadata", mode="before")
    @classmethod
    def _metadata_in(cls, v):
        """
        Accept dict, None, or JSON string and normalize to dict/None.
        Same semantics as other *_with_uploads commands.
        """
        if v is None or isinstance(v, dict):
            return v

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

            return {"value": parsed}

        try:
            return dict(v)
        except Exception:
            raise ValueError("metadata must be a JSON object or JSON string")


# ---------------- Command ----------------

class UpdateNavigationsWithUploadsCommand(BaseCommand):
    """
    Partially updates a CompNavigation row:
      - name, description, template_id, tags, metadata_json, thumbnail, images, file_link,
        group_id, sub_type, updated_by

    File uploads (multipart/form-data):
      - thumbnail: UploadFile (single)  -> stored as GCS key in 'thumbnail'
      - images: List[UploadFile]        -> stored as list of GCS keys in 'images'
      - file: UploadFile                -> stored as GCS key in 'file_link'
    """
    name = "components/navigations/update_with_uploads"
    schema = UpdateCompNavigationsPayload
    require_auth = True
    method = "put"
    type = "file_upload"
    group = "Navigation"

    file_fields = [
        ("thumbnail", False),
        ("images", True),
        ("file", False),  # NEW: single file
    ]

    base_folder = "uploads"

    async def execute(
        self,
        payload: UpdateCompNavigationsPayload,
        thumbnail: Optional[UploadFile] = None,
        images: Optional[List[UploadFile]] = None,
        file: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ):
        start_t = time.monotonic()
        logger.info("[navigations.update] start id=%s user_id=%s", payload.id, user_id)

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        db = SessionLocal()
        try:
            # NOTE: Query.get() is legacy in SA 2.x, but keeping your style consistent
            row: Optional[CompNavigation] = db.query(CompNavigation).get(payload.id)
            if not row:
                raise HTTPException(status_code=404, detail="Navigation not found")

            gcs = get_gcs()
            dest_prefix = f"{self.base_folder}/{row.id}"

            async def _read_and_check(
                f: UploadFile,
                max_mb: int,
                allowed: Optional[set],
            ):
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
                    "[navigations.update] _read file=%s size=%d ct=%s elapsed=%.3fs",
                    getattr(f, "filename", None),
                    size,
                    ct,
                    time.monotonic() - t0,
                )
                if size == 0:
                    raise ValueError(f"File '{f.filename}' is empty")
                if size > max_mb * 1024 * 1024:
                    raise ValueError(f"File '{f.filename}' exceeds {max_mb}MB limit")
                if allowed is not None and ct not in allowed:
                    raise ValueError(f"Unsupported content type '{ct}' for '{f.filename}'")
                return data, ct

            async def _upload_one(
                f: Optional[UploadFile],
                subfolder: str,
                default_stem: str,
                image: bool = False,
            ):
                if not f:
                    return None
                if image:
                    _, ct = await _read_and_check(f, MAX_IMAGE_MB, IMAGE_TYPES)
                else:
                    _, ct = await _read_and_check(f, MAX_FILE_MB, ANY_FILE_TYPES)

                fname = make_uuid_name(f.filename, default_stem)
                res = await asyncio.to_thread(
                    gcs.upload_fileobj,
                    f.file,
                    fname,
                    f"{dest_prefix}/{subfolder}",
                    False,
                    _resolve_content_type(f),
                )

                try:
                    f.file.seek(0, os.SEEK_END)
                    size = f.file.tell()
                    f.file.seek(0)
                except Exception:
                    size = None

                return {
                    "key": res["key"],
                    "filename": f.filename,
                    "content_type": _resolve_content_type(f),
                    "size": size,
                }

            # ------------ scalar fields ------------
            if payload.name is not None and payload.name.strip():
                row.name = payload.name.strip()
            if payload.description is not None:
                row.description = payload.description
            if payload.template_id is not None:
                row.template_id = payload.template_id

            # NEW
            if payload.group_id is not None:
                row.group_id = payload.group_id
            if payload.sub_type is not None:
                row.sub_type = payload.sub_type

            # ------------ tags ------------
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
                if payload.tags_mode == "replace" and not payload.tags_clear:
                    pass

            if payload.tags_clear or payload.tags is not None or row.tags is None:
                row.tags = current_tags

            # ------------ metadata_json ------------
            if payload.metadata_clear:
                row.metadata_json = None
            elif payload.metadata is not None:
                row.metadata_json = payload.metadata or None

            # ------------ thumbnail (key) ------------
            if payload.thumbnail_clear:
                row.thumbnail = None
            if thumbnail:
                _, ct = await _read_and_check(thumbnail, MAX_IMAGE_MB, IMAGE_TYPES)
                fname = make_uuid_name(thumbnail.filename, "thumbnail")
                res = await asyncio.to_thread(
                    gcs.upload_fileobj,
                    thumbnail.file,
                    fname,
                    f"{dest_prefix}/thumbnail",
                    False,
                    ct,
                )
                row.thumbnail = res["key"]

            # ------------ images (list of keys) ------------
            current_images: List[str] = list(row.images or [])
            if payload.images_clear:
                current_images = []

            new_image_keys: List[str] = []
            if images:
                for idx, img in enumerate(images):
                    _, ct = await _read_and_check(img, MAX_IMAGE_MB, IMAGE_TYPES)
                    fname = make_uuid_name(img.filename, f"img{idx:03d}")
                    res = await asyncio.to_thread(
                        gcs.upload_fileobj,
                        img.file,
                        fname,
                        f"{dest_prefix}/images",
                        False,
                        ct,
                    )
                    if res and res.get("key"):
                        new_image_keys.append(res["key"])

            if new_image_keys:
                if payload.images_mode == "replace":
                    current_images = new_image_keys
                else:
                    current_images.extend(new_image_keys)

            if payload.images_clear or new_image_keys or (row.images is None):
                row.images = current_images

            # ------------ file_link (single TEXT key) ------------
            if payload.file_link_clear:
                row.file_link = None

            if file:
                try:
                    meta = await _upload_one(file, "nav", "nav", image=False)
                    if meta and meta.get("key"):
                        row.file_link = meta["key"]
                except Exception as e:
                    logger.exception("[navigations.update] upload failed for file")
                    raise HTTPException(status_code=400, detail=str(e))

            # updated_by
            row.updated_by = user_id

            # ------------ persist ------------
            t0 = time.monotonic()
            db.add(row)
            db.commit()
            db.refresh(row)
            logger.info(
                "[navigations.update] DB commit elapsed=%.3fs id=%s",
                time.monotonic() - t0,
                row.id,
            )

            # ------------ signed URLs (best-effort) ------------
            signed = {
                "thumbnail": None,
                "images": [],
                "file_link": None,
                "row_id": str(row.id),
            }

            if row.thumbnail:
                try:
                    signed["thumbnail"] = gcs.signed_get_url(row.thumbnail, expires_seconds=3600)
                except Exception:
                    signed["thumbnail"] = f"https://storage.googleapis.com/{gcs.bucket_name}/{row.thumbnail}"

            for key in row.images or []:
                try:
                    signed["images"].append(gcs.signed_get_url(key, expires_seconds=3600))
                except Exception:
                    signed["images"].append(f"https://storage.googleapis.com/{gcs.bucket_name}/{key}")

            if row.file_link:
                try:
                    signed["file_link"] = gcs.signed_get_url(row.file_link, expires_seconds=3600)
                except Exception:
                    signed["file_link"] = f"https://storage.googleapis.com/{gcs.bucket_name}/{row.file_link}"

            return {
                "status": "ok",
                "data": _comp_nav_to_dict(row),
                "gcs": signed,
                "perf_ms": round((time.monotonic() - start_t) * 1000, 2),
            }

        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            logger.exception("[navigations.update] error - rolling back: %s", e)
            db.rollback()
            raise HTTPException(status_code=500, detail="Failed to update navigation")
        finally:
            db.close()
            # close any open file handles
            for uf in [thumbnail] + (images or []) + [file]:
                try:
                    if uf and getattr(uf, "file", None) and not uf.file.closed:
                        uf.file.close()
                except Exception:
                    pass


# ---------- serializer ----------

def _comp_nav_to_dict(m: CompNavigation) -> dict:
    return {
        "id": str(m.id) if getattr(m, "id", None) is not None else None,
        "name": m.name,
        "description": m.description,
        "thumbnail": getattr(m, "thumbnail", None),
        "images": getattr(m, "images", None),
        "template_id": str(m.template_id) if getattr(m, "template_id", None) else None,

        # UPDATED
        "file_link": getattr(m, "file_link", None),

        # NEW
        "group_id": str(getattr(m, "group_id", None)) if getattr(m, "group_id", None) else None,
        "sub_type": getattr(m, "sub_type", None),

        "tags": getattr(m, "tags", []) or [],
        "metadata": getattr(m, "metadata_json", None) or {},
        "created_by": m.created_by,
        "updated_by": m.updated_by,
        "created_at": m.created_at.isoformat() if getattr(m, "created_at", None) else None,
        "updated_at": m.updated_at.isoformat() if getattr(m, "updated_at", None) else None,
    }
