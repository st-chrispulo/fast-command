# commands/components/navigations/create_with_uploads.py

import os
import re
import json
import mimetypes
import time
import asyncio
from uuid import uuid4
from typing import Optional, List, Any, Dict
from uuid import UUID

from fastapi import UploadFile, HTTPException
from pydantic import BaseModel, field_validator

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_navigations import CompNavigation

# Prefer your app logger if available; fallback to stdlib
try:
    from logger import logger
except Exception:
    import logging as _logging
    logger = _logging.getLogger("navigations_create_with_uploads")
    if not logger.handlers:
        handler = _logging.StreamHandler()
        handler.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(_logging.DEBUG)

# ---------- helpers ----------

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


def make_uuid_name(filename: str, default_stem: str) -> str:
    name = filename or ""
    stem, ext = os.path.splitext(name)
    stem = (stem or default_stem).strip()
    stem = _SAFE_CHARS_RE.sub("_", stem) or default_stem
    ext = (ext or "").lower().lstrip(".") or "bin"
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
    items, seen = [], set()
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            items.append(p)
    return items


# ---------- payload ----------

class CreateCompNavigationsPayload(BaseModel):
    name: str
    description: Optional[str] = None
    template_id: Optional[UUID] = None
    tags: Optional[str] = None  # comma-separated
    # metadata (JSONB -> metadata_json)
    metadata: Optional[Any] = None

    # NEW (per model/migration)
    group_id: Optional[UUID] = None
    sub_type: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _name_trim(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("name is required")
        return v

    @field_validator("description", mode="before")
    @classmethod
    def _strip_optional(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("tags", mode="before")
    @classmethod
    def _tags_in(cls, v):
        if v is None:
            return None
        return str(v).strip()

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

            metadata = '{"layoutType":"nav","outputs":[...]}'
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


# ---------- command ----------

class CreateCompNavigationsWithUploadsCommand(BaseCommand):
    """
    Creates a CompNavigation with optional uploads:

    Files (multipart/form-data):
      - thumbnail: UploadFile (single)  -> saved to 'thumbnail' (TEXT key)
      - images: List[UploadFile]        -> saved to 'images' (JSONB array of keys)
      - file: UploadFile                -> saved to 'file_link' (TEXT key)

    Also stores optional metadata -> metadata_json (JSONB),
    plus group_id/sub_type.
    """
    name = "components/navigations/create_with_uploads"
    schema = CreateCompNavigationsPayload
    require_auth = True
    method = "post"
    type = "file_upload"
    group = "Navigation"

    # Swagger/OpenAPI exposure
    file_fields = [
        ("thumbnail", False),      # single image
        ("images", True),          # multiple images
        ("file", False),           # single nav file (replaces file_links.top/side/bottom)
    ]

    base_folder = "uploads"

    async def execute(
        self,
        payload: CreateCompNavigationsPayload,
        thumbnail: Optional[UploadFile] = None,
        images: Optional[List[UploadFile]] = None,
        file: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ):
        t0 = time.monotonic()
        logger.info("[navigations] start name=%s user_id=%s", getattr(payload, "name", None), user_id)
        gcs = get_gcs()

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        row_id = str(uuid4())
        dest_prefix = f"{self.base_folder}/{row_id}"

        async def _read_and_check_img(f: UploadFile):
            if f is None:
                return None, None
            data = await f.read()
            try:
                f.file.seek(0)
            except Exception:
                pass
            if len(data) > MAX_IMAGE_MB * 1024 * 1024:
                raise ValueError(f"Image '{f.filename}' exceeds {MAX_IMAGE_MB}MB limit")
            ct = _resolve_content_type(f)
            if ct not in IMAGE_TYPES:
                raise ValueError(f"Unsupported image type '{ct}' for '{f.filename}'")
            return data, ct

        async def _read_and_check_any(f: UploadFile):
            if f is None:
                return None, None
            data = await f.read()
            try:
                f.file.seek(0)
            except Exception:
                pass
            if len(data) > MAX_FILE_MB * 1024 * 1024:
                raise ValueError(f"File '{f.filename}' exceeds {MAX_FILE_MB}MB limit")
            ct = _resolve_content_type(f)
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
                _, ct = await _read_and_check_img(f)
            else:
                _, ct = await _read_and_check_any(f)

            fname = make_uuid_name(f.filename, default_stem)
            res = await asyncio.to_thread(
                gcs.upload_fileobj,
                f.file,
                fname,
                f"{dest_prefix}/{subfolder}",
                False,
                ct,
            )

            # try to get size
            try:
                f.file.seek(0, os.SEEK_END)
                size = f.file.tell()
                f.file.seek(0)
            except Exception:
                size = None

            return {
                "key": res["key"],
                "filename": f.filename,
                "content_type": ct,
                "size": size,
            }

        # ---- upload thumbnail ----
        thumbnail_key: Optional[str] = None
        if thumbnail:
            meta = await _upload_one(thumbnail, "thumbnail", "thumbnail", image=True)
            thumbnail_key = meta["key"] if meta else None

        # ---- upload images[] ----
        images_keys: Optional[List[str]] = None
        if images:
            images_keys = []
            for idx, img in enumerate(images):
                meta = await _upload_one(img, "images", f"img{idx:03d}", image=True)
                if meta:
                    images_keys.append(meta["key"])

        # ---- upload single nav file -> file_link ----
        file_link_key: Optional[str] = None
        file_meta: Optional[Dict[str, Any]] = None
        if file:
            try:
                file_meta = await _upload_one(file, "nav", "nav", image=False)
                file_link_key = file_meta["key"] if file_meta else None
            except Exception as e:
                logger.exception("[navigations] upload failed for file")
                raise HTTPException(status_code=400, detail=str(e))

        # ---- persist ----
        db = SessionLocal()
        try:
            row = CompNavigation(
                name=payload.name,
                description=payload.description,
                template_id=payload.template_id,
                thumbnail=thumbnail_key,
                images=images_keys,
                file_link=file_link_key,
                group_id=payload.group_id,
                sub_type=payload.sub_type,
                created_by=user_id,
                updated_by=user_id,
                tags=_normalize_tags(payload.tags),
                metadata_json=payload.metadata or None,
            )

            db.add(row)
            db.commit()
            db.refresh(row)

            # ---- signed urls (best-effort) ----
            signed = {
                "thumbnail": None,
                "images": [],
                "file_link": None,
                "row_id": row_id,
            }

            if thumbnail_key:
                try:
                    signed["thumbnail"] = gcs.signed_get_url(thumbnail_key, expires_seconds=3600)
                except Exception:
                    signed["thumbnail"] = f"https://storage.googleapis.com/{gcs.bucket_name}/{thumbnail_key}"

            for k in images_keys or []:
                try:
                    signed["images"].append(gcs.signed_get_url(k, expires_seconds=3600))
                except Exception:
                    signed["images"].append(f"https://storage.googleapis.com/{gcs.bucket_name}/{k}")

            if file_link_key:
                try:
                    signed["file_link"] = gcs.signed_get_url(file_link_key, expires_seconds=3600)
                except Exception:
                    signed["file_link"] = f"https://storage.googleapis.com/{gcs.bucket_name}/{file_link_key}"

            return {
                "status": "ok",
                "data": _comp_nav_to_dict(row),
                "gcs": signed,
                "file_meta": file_meta,  # keep if your UI needs filename/content_type/size
                "perf_ms": round((time.monotonic() - t0) * 1000, 2),
            }
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            logger.exception("[navigations] DB error")
            raise
        finally:
            db.close()
            # close any open file handles
            for uf in [thumbnail] + (images or []) + [file]:
                try:
                    if uf and getattr(uf, "file", None) and not uf.file.closed:
                        uf.file.close()
                except Exception:
                    pass


def _comp_nav_to_dict(m: CompNavigation) -> dict:
    return {
        "id": str(m.id) if getattr(m, "id", None) is not None else None,
        "name": m.name,
        "description": m.description,
        "thumbnail": getattr(m, "thumbnail", None),
        "images": getattr(m, "images", None),
        "template_id": str(m.template_id) if getattr(m, "template_id", None) else None,

        # UPDATED: single file link
        "file_link": getattr(m, "file_link", None),

        # NEW
        "group_id": str(m.group_id) if getattr(m, "group_id", None) else None,
        "sub_type": getattr(m, "sub_type", None),

        "tags": getattr(m, "tags", []) or [],
        "metadata": getattr(m, "metadata_json", None) or {},
        "created_by": m.created_by,
        "updated_by": m.updated_by,
        "created_at": m.created_at.isoformat() if getattr(m, "created_at", None) else None,
        "updated_at": m.updated_at.isoformat() if getattr(m, "updated_at", None) else None,
    }
